# Security policy

`audr` is a self-hosted Ethereum portfolio tracker. It never asks for a private
key and cannot move funds, but an installation does hold things worth protecting:
the set of addresses an owner watches, their balance history, a session cookie,
and provider credentials (RPC URLs, API keys) encrypted at rest. Please treat
anything that exposes those as a security issue.

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.** Use GitHub's private
reporting instead: the **Security** tab → **Report a vulnerability**, which opens
a private advisory visible only to the maintainers. If private reporting is
unavailable to you, open a public issue that says only "requesting a private
channel for a security report" — with no details — and a maintainer will open
one.

Useful to include, as far as you have it:

- affected version (git SHA or image tag) and how the stack is exposed
  (localhost, LAN, behind a reverse proxy);
- what an attacker gains, not only what misbehaves;
- a minimal reproduction — a `curl` sequence is ideal;
- whether authentication is required, and whether it crosses the owner boundary
  (audr is single-owner by design, so anything that lets an unauthenticated
  caller read or write owner data is high severity).

This is a small project with no on-call rotation. Expect an acknowledgement
within about a week, and a fix timeline in that reply. There is no bounty
programme.

## Scope

In scope — the code in this repository:

- the FastAPI backend, worker and migrations under `backend/`;
- the SPA under `frontend/`;
- authentication, session handling and the CSRF guards;
- secret handling: the master key, the AES-256-GCM encryption of provider
  credentials, and redaction in logs, API responses and exports;
- the SSRF validation applied to owner-supplied RPC endpoints;
- the deployment surface in `compose.yaml`, `Dockerfile` and `scripts/`.

Out of scope:

- vulnerabilities in third-party dependencies with no audr-specific
  exploitation path — report those upstream (`docs/third-party.md` lists what we
  pin);
- the session cookie lacking the `Secure` flag. This is a documented
  consequence of serving plain HTTP internally; TLS belongs to the reverse proxy
  in front of the stack. An installation exposed to the internet without HTTPS
  is a deployment mistake, not a code defect.
- an installation deliberately exposed to the internet with no proxy or network
  controls in front of it;
- the fact that a configured RPC or quote provider observes the requests sent to
  it. Self-hosting removes the vendor, not the network; each integration
  documents what it receives.

## What audr does and does not hold

- **No private keys, seed phrases or signing material. Ever.** Adding an address
  asserts nothing about who controls it, and there is no code path that spends.
- Provider credentials are encrypted with AES-256-GCM under a master key wrapped
  by `SECRET_KEY`, and redacted wherever they could otherwise surface.
- Addresses, balances and history are **not** encrypted at the column level. The
  threat model, and the volume encryption recommended alongside it, are in
  [`docs/security-at-rest.md`](docs/security-at-rest.md).

## Supported versions

Only `main` receives security fixes. There are no long-lived release branches: if
you are running an older image, the upgrade path is the current `main`.

## Hardening an installation

- Keep audr off the public internet unless you put HTTPS and an authenticating
  proxy in front of it.
- Back up `secrets/master_key.hex`. It is not recoverable, and losing it means
  re-entering every provider credential — see
  [key-loss behavior](docs/operations.md#key-loss-behavior).
- Encrypt the volume holding the database (`docs/security-at-rest.md`).
- Keep `.env` and `secrets/` out of version control. Both are gitignored here,
  and no credential has ever been committed to this repository.
