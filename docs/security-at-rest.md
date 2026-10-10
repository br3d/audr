# Encryption at rest

> **Question this document answers:** what does audr encrypt on disk, what does
> it leave in the clear, and what should you do about the difference?

SQLite-based trackers can encrypt the whole database file with SQLCipher. audr
runs on PostgreSQL, where there is no equivalent; what it does instead, and the
reasoning behind that choice, is below. The design alternatives that were
weighed are kept separately in
[security-at-rest-design.md](security-at-rest-design.md).

## 1. What is encrypted

audr uses **application-level envelope encryption**, described operationally
under [key-loss behavior](operations.md#key-loss-behavior):

| Layer | What it is | Where the key lives |
| --- | --- | --- |
| KEK | 64-hex `SECRET_KEY`, from `secrets/master_key.hex` | Host filesystem / env var, **outside** the DB |
| Master key | Random 32-byte AES-256 key, wrapped by the KEK | `key_state.wrapped_key` in Postgres |
| Payloads | AES-256-GCM, versioned envelope `version(1) || nonce(12) || ct+tag` | `backend/src/audr/operations/crypto.py` |

What it covers:

- provider credentials — RPC URLs, quote-provider API keys, and the
  `allow_private_host` flag (`integration.encrypted_blob`, see
  `backend/src/audr/settings/integrations.py`)
- `wallet.label` (`wallet.label_ciphertext`, AUD-488) — the only free-text,
  owner-written column in the schema, and the only wallet column that is
  never filtered, sorted or joined on, which is what makes it encryptable at
  all (see §2 D below on why the others are not)
- `wallet.address` (`wallet.address_ciphertext` + `wallet.address_bidx`,
  AUD-490) — the last owner-identifying column. Its `unique=True` constraint
  moved to `address_bidx`, a deterministic HMAC-SHA256 blind index keyed by a
  subkey derived from the master key (not the master key itself), which is
  what makes a column that *is* filtered and joined on (unlike the label)
  encryptable without losing lookup/uniqueness. See item 3b in
  [security-at-rest-design.md](security-at-rest-design.md#3-where-that-leaves-the-backlog).

What is **plaintext on disk**:

- holdings, balances, valuation snapshots and lines (`valuation_snapshot`,
  `valuation_line`)
- the full price/quote history (`quote_set`, `quote_observation`)
- the asset catalog, on-chain event index, news cache
- the owner's Argon2/bcrypt password hash and session rows (hashes, by design)

For a portfolio tracker that plaintext set *is* the sensitive set: net worth,
every address you own, and the link between them. Treat the database volume
accordingly.

## 2. What to do about it

**Encrypt the volume (recommended baseline).** Put the block device or dataset
backing the `db_data` Docker volume on LUKS or a ZFS native-encrypted dataset.

- **Protects against:** a stolen disk, a stolen laptop or NAS, a decommissioned
  drive, a cloud snapshot or backup tarball leaving the host.
- **Does not protect against:** anything while the machine is running — root,
  the Docker daemon, a compromised application, or `docker exec … psql`.
- **Cost:** host setup only; no application or schema change. AES-NI makes the
  throughput cost a few percent. It needs a passphrase at boot, or a
  keyfile/TPM2 unlock if the box must reboot unattended.

This is the honest answer to "what is audr's SQLCipher". It is also what the
PostgreSQL community itself recommends, community PostgreSQL having no
transparent data encryption of its own.

The operator procedure — the `compose.encrypted-volume.yaml` overlay, and why
this is a first-time-setup decision rather than a toggle you flip later — is in
[operations.md](operations.md#encrypting-the-database-volume).

**Keep backups encrypted.** `scripts/backup.sh` pipes `pg_dump` through `age`
(or `gpg --symmetric` when `age` is unavailable) and never writes a plaintext
dump to disk; it generates a recipient key on first run so the first backup is
already encrypted. `secrets/master_key.hex` is captured alongside the dump in a
separate, deliberately un-bundled file. The operator procedure is in
[operations.md#backups](operations.md#backups).

**Know the limit of the current model.** `SECRET_KEY` sits in a file on the
same host as the database, so whoever takes the whole running machine takes
both halves, and volume encryption only helps while the machine is powered off.
Deriving the key from the owner's login password instead would change that, at
the cost of background jobs that can only run while someone is logged in; the
trade-off is written up in
[security-at-rest-design.md](security-at-rest-design.md).

## 3. Handling key material: never echo a secret value

Everything above protects keys *at rest in the database*. It does nothing about
the much easier leak: a key value copied into a place nobody thinks of as a
secret store. Command output, log lines, issue comments, documents, commit
messages, test fixtures — and the one that catches people most often, shell
commands whose arguments are captured in history or visible in `ps`.

**So: never write a secret value anywhere it will be recorded.** When you need
to talk about *which* key you hold, print a fingerprint:

```bash
printf '%s' "$SECRET_KEY" | sha256sum | head -c 12
```

Twelve hex characters of SHA-256 is enough to confirm two parties hold the same
key, or that a rotation changed it, and reveals nothing. The rotation command
follows the same rule — it prints a fingerprint, never a key value.

Two corollaries that are easy to get wrong:

- **Pass secrets in files, not arguments.** `docker run -e KEY=<value>` writes
  the value into your shell history and exposes it in `ps` for the container's
  lifetime. Use `umask 077` + `--env-from-file`, then `shred -u` the file.
- **Do not leave rollback copies behind.** A `.env` backup, or a tarball of a
  deploy directory, is key material at rest in an unmanaged place. If you make
  one, shred it in the same session.

If a value does leak, rotate it **before** scrubbing copies of it, and bear in
mind that a scan for it over database files or compressed dumps can return zero
hits while the value is still there — see
[verification-history.md](verification-history.md#why-grep-over-postgres-files-and-dumps-is-not-evidence).

### Rotating the KEK

A leaked `SECRET_KEY` cannot simply be edited in `.env`. It is a
key-encryption-key: it wraps the master key stored in `key_state`, so changing
it without re-wrapping the blob makes every stored credential undecryptable and
fails `/health/ready` (`key`). `backend/tests/integration/test_restart.py`
asserts that failure deliberately.

Use the rotation operation instead
(`backend/src/audr/operations/init_key.py`). It decrypts the blob with the old
KEK and re-encrypts it with the new one in a single transaction; the raw master
key never changes, so existing ciphertext stays readable and nothing needs
re-encrypting. It is idempotent — if the blob already decrypts under the new
KEK it is a no-op — and it refuses a wrong old KEK rather than writing a
corrupt blob.

```bash
umask 077 && cat > rotate.env <<'EOF'
OLD_SECRET_KEY=...
NEW_SECRET_KEY=...
EOF
docker compose run --rm --env-from-file rotate.env \
    migrate python -m audr.operations.init_key rotate
shred -u rotate.env
```

The flag is `--env-from-file`, not `--env-file`. `docker compose run` has no
`--env-file` and exits 1 with `unknown flag`; the similarly-named *global*
`docker compose --env-file` only feeds variable interpolation of the compose
file and would not put `OLD_SECRET_KEY` inside the container, so the mistake is
worse than a typo — it can look like it ran.

The operation reads `OLD_SECRET_KEY`/`NEW_SECRET_KEY` rather than `SECRET_KEY`
so a half-configured environment cannot run it by accident. Rollback is the
same command with the two values swapped.

## References

- [PostgreSQL wiki — Transparent Data Encryption](https://wiki.postgresql.org/wiki/Transparent_Data_Encryption)
- [pgEdge — Why Postgres lacks TDE](https://www.pgedge.com/blog/why-postgres-lacks-transparent-data-encryption)
- [Percona — pg_tde documentation](https://docs.percona.com/pg-tde/)
- [Percona — pg_tde is ready for production](https://www.percona.com/blog/the-pg_tde-extension-is-now-ready-for-production/)
