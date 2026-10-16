# Deploy runbook (audr stack on the self-hosted deploy host)

This repository is public, so it records no host addresses. Throughout this
document `$AUDR_DEPLOY_HOST` is the `user@host` of the deploy host and
`$AUDR_REGISTRY` the `host:port` of the image registry; both come from an
untracked `deploy.env` at the repo root (copy `deploy.env.example`) or from the
environment, and the CI workflows read the registry from the Gitea repository
variable `AUDR_REGISTRY`.

The deploy pipeline is a single guarded Gitea Actions job, `.gitea/workflows/deploy.yaml`.
The file you edit is `ci/gitea-overlay/workflows/deploy.yaml` in this repository,
and it is the copy that **runs**: `~/bin/audr-github-mirror.sh` reads it out of
the GitHub `main` commit it is mirroring and bakes it into the Gitea `main` commit
as `.gitea/workflows/deploy.yaml`. A workflow change therefore ships like any
other change — commit, push to `main`, wait for the next mirror tick (15 min).

They are deliberately not tracked at `.gitea/workflows/` because the mirror pushes
every non-`main` branch verbatim, and Gitea would then replay the suite for every
stale branch it syncs (see `ci/gitea-overlay/README.md`).

**The mirror unit looks broken for up to 15 minutes after every reboot, and the
drop-in that fixes it is not shipped by the commit.** `audr-mirror.timer` is
`Persistent=true`, so on boot systemd replays the missed window immediately —
before DNS answers, even though the unit has `After=network-online.target`
(which only means an address is configured, not that resolution works). The
oneshot dies on `Could not resolve hostname github.com` and, having no
`Restart=`, sits in `failed` until the next tick. The sync itself recovers on its
own; what does not is `systemctl --failed`, which then cannot tell this apart
from a mirror that is genuinely broken — a revoked deploy key, or Gitea down.
`ci/host-units/audr-mirror-dns-race.conf` waits for resolution and retries
instead, and reaches the host only via

    scripts/install-mirror-dropin.sh          # --check to report, no changes

Run `--check` first if `audr-mirror.service` is `failed` shortly after a reboot:
if it reports the drop-in absent, that failure is the boot race, not an incident.

The host overlay directory (`~/.config/audr-mirror/overlay/.gitea/workflows/`) is
now only a **fallback** for files the commit does not carry; it cannot override
one that it does. `scripts/sync-ci-overlay.sh --check` still diffs the two and
exits 1 on a difference — worth running if you suspect a host-only file is in
play, but no longer a step in shipping a workflow change. Before AUD-443 the host
copy won, and a forgotten sync silently ran a stale `deploy.yaml` that took the
stand down for two hours.

## Invariants the pipeline depends on

**The deploy runs two compose files.** `compose.yaml` is the public install: it
pulls `ghcr.io/br3d/audr-backend:<version>` literally and knows nothing about
our registry. `compose.deploy.yaml` is the overlay that redirects the three
backend services at `${AUDR_REGISTRY}/audr-backend:${BACKEND_TAG}`, and both
variables are required — a missing one fails the deploy instead of silently
pulling the public release. The deploy job and `scripts/deploy.sh` copy both
files into `~/audr/` and pin

    COMPOSE_FILE=compose.yaml:compose.deploy.yaml

in `~/audr/.env`, so a **manual** `docker compose` run in that directory
resolves the same images the pipeline deployed. If a hand-run compose command
there ever tries to pull from `ghcr.io`, that line is missing (AUD-439).

**Images are pinned, never floated.** The overlay resolves `${BACKEND_TAG}`
from `~/audr/.env`, and the deploy job pins it to the build sha. This is not
cosmetic: `docker compose up -d` decides whether to recreate a service by
comparing the service *definition*, not the image ID a floating tag currently
resolves to. While the deployed image was `...:latest`, every deploy
retagged `:latest`, ran the migrations, passed the health-gate **against the
previous containers** and reported `Deploy OK` while the running code never
changed. Step 5b of the job now asserts that `audr-api-1` and `audr-worker-1`
really run the new tag before the health-gate result is trusted.

There is one image and one tag variable since AUD-388 removed the
`audr-frontend` image. A stale `FRONTEND_TAG=` line may still sit in
`~/audr/.env`; nothing interpolates it, and it is safe to delete or ignore.

**A rollback must restore the schema, not just the images.** Restoring images
alone is not a restorable state: a deploy that migrated the DB and then failed
leaves the schema ahead of the old image, whose `alembic upgrade head` dies with
`Can't locate revision identified by '<new>'`. The job snapshots the pre-deploy
alembic revision and, on failure, downgrades back to it **using the new image**
(the only one that knows how to reverse its own revisions) before restoring the
tags. If that downgrade is impossible the old tags are deliberately *not*
restored and the stack stays on the new build — a running-but-newer stack beats
a guaranteed-dead one — and the job fails loudly for a human decision.

**One-shots must not restart.** `migrate` and `init` are `restart: "no"`.
`init`/`api` wait on them with `service_completed_successfully`, so an unbounded
`restart: on-failure` turns a failed migration into a `docker compose up` that
blocks forever instead of failing.

## Three CI-only gotchas, each of which has caused an outage or a silent no-op

- **`timeout --foreground`** — plain GNU `timeout` puts the child in a new
  process group, so a docker CLI that writes to the terminal takes `SIGTTOU` and
  is *stopped* (`ps` state `T`), hanging the job forever: the exact failure the
  timeout was added to prevent.
- **`docker compose run -T`** — never allocate a TTY in a CI job.
- **Exported variables beat `.env`** — the job sources `.env` for `DB_PASSWORD`,
  which exports the *old* `BACKEND_TAG`. Compose gives the environment
  precedence over the `.env` file, so rewriting the file alone leaves compose
  resolving the stale tag and skipping the recreate. `env_set` writes the file
  **and** exports.

## Deploying by hand with `scripts/deploy.sh`

The pipeline is the normal path; `./scripts/deploy.sh` is the same deploy driven
from a workstation, for when the runner is unavailable. It needs the SSH key at
`./id_ed25519` (mode `600`) and Docker Engine 26+ on the deploy host, and it:

1. Copies `compose.yaml` and the private-registry overlay `compose.deploy.yaml`
   to the host over SCP, and pins
   `COMPOSE_FILE=compose.yaml:compose.deploy.yaml` in the remote `.env` so
   manual compose commands there resolve the same images
2. Pulls the current images from `$AUDR_REGISTRY`
3. Runs `alembic upgrade head` via the `migrate` service
4. Restarts the stack with `docker compose up -d --remove-orphans`
5. Polls `/health` until healthy (60 s timeout)

This is our deployment of our instance, not a self-hosting procedure — an
operator running their own instance has no access to this registry and installs
from `compose.yaml` alone, as [operations.md](operations.md) describes.

## Stand down after a failed deploy

A red deploy usually means nothing is wrong with the stand: most of the guarded
job's failures happen before anything destructive, and the ones that do not
normally end in a rollback that leaves the previous release serving. So the run
status alone does not tell you whether to drop everything.

The **Report stand state (failure path)** step at the end of every failed deploy
answers exactly that question, and since AUD-444 it also sends the DOWN verdict
to Telegram so you do not have to be looking at the run to learn about it (see
[Alerting](#alerting-how-you-find-out-the-stand-is-down)). Read it first:

- `[stand] deploy FAILED but the stand is serving` — no outage. Fix the build or
  the gate at human speed.
- `[stand] DEPLOY FAILED AND THE STAND IS DOWN` — audr is not answering. Act now.

That second line is the one AUD-443 did not have. Run 447 failed correctly, the
rollback could not recover because it resolved images through the same broken
compose, and the stand served nothing but Postgres for two hours because the job
log ended at `migrate` exiting 255 and nobody read further.

When the stand is down, the cause is almost always one of two things:

1. **Compose resolved the wrong image.** Since AUD-443 the deploy asserts this
   up front, so a current pipeline fails in a second with `the
   compose.deploy.yaml overlay did not take effect`. On the host, confirm with
   `cd ~/audr && docker compose config --images` — every `audr-backend` line
   must be `$AUDR_REGISTRY/audr-backend:<tag>`, never `ghcr.io/...`. If it is
   not, check `COMPOSE_FILE=compose.yaml:compose.deploy.yaml` in `~/audr/.env`
   and that both compose files are present.
2. **The schema and the image disagree** — the next section.

Note that 1 *causes* 2: the public image's alembic tree lags the live DB, so a
silent downgrade presents as a migration failure. Fix the image resolution
before touching the schema; downgrading a schema to match an image that was
never meant to run is how a bad deploy becomes a bad database.

## Alerting: how you find out the stand is down

AUD-443's two hours were not deploy time, they were nobody-noticing time. Two
independent paths now push that fact out of a log and onto the board, and they
cover deliberately different failure shapes:

| | fires when | threshold | lives in |
|---|---|---|---|
| CI failure step | a deploy fails **and** leaves `/health/ready` not-ok | none — immediate | `ci/gitea-overlay/workflows/deploy.yaml` |
| `audr-watchdog.timer` | `/health/ready` fails 5 probes in a row, deploy or no deploy | ~5 min sustained | `scripts/stand-watchdog.sh` on the host |
| `audr-backup.timer` | the daily dump fails, or its own decrypt-check says the archive is unreadable | one run — immediate | `scripts/backup-run.sh` on the host |
| `audr-watchdog.timer` (backup freshness) | the newest dump in `backups/` is older than 26h, or there is none at all | ~26h since the last dump | `check_backup_age` in `scripts/stand-watchdog.sh` |

The last two rows look redundant and are not. `backup-run.sh` can only speak if
it runs: a backup that **fails** alerts, a backup that **never starts** — timer
disabled by a daemon-reload, unit file lost to a reinstall, `OnCalendar` mistyped
— is completely silent. That silence is the shape of AUD-562 (the dump tooling
was shipped and proven, then nothing ran it for two weeks) and of AUD-505 (a
timer authorised and closed while the unit did not exist). So the freshness check
watches the **artefact's mtime**, not the schedule, and rides the watchdog timer
that is observably firing every 60s rather than a second timer that would need
its own liveness proof. 26h, not 24h, because 03:20 UTC + `RandomizedDelaySec=10m`
can legitimately put two dumps 24h20m apart; a missed 03:20 still surfaces by
~05:30 the same morning. Alert key `backup-stale`, one message a day while stale,
one `✅` that closes the issue when a dump lands.

The watchdog is the one that matters most, because the CI step structurally
cannot see an outage that no deploy caused — an OOM kill, a reboot, Postgres
dying on its own produce no CI run at all. It probes every 60s, alerts on the
fifth consecutive failure, repeats hourly while still down, and sends one
`✅ RECOVERED` message when `/health/ready` returns ok again — which also closes
the board issue, so the responder is told to stand down rather than left to
discover it.

It does not fire on the first failed probe on purpose: a single miss is
routinely just a container restarting mid-deploy, and paging on it teaches the
one person on call to mute the channel, which costs more than the five minutes
it saves. Target human response is 15 minutes.

### Channel: a `critical` board issue

Chosen on AUD-444 over Telegram, email and a hosted uptime prober. The deciding
property is that a board issue **wakes its assignee**: noticing becomes a
timer's job instead of someone's attention, which is the exact thing that was
missing for two hours. A chat message shares the failure mode it is meant to
fix — it only helps if a human happens to be looking.

A hosted prober (healthchecks.io, Uptime Kuma) was ruled out on topology: the
stand is on a LAN address, so an external prober needs ingress to
`192.168.1.228` plus a firewall change just to receive a heartbeat.

Telegram remains implemented as a second, independent sink, because it needs no
ingress either — one outbound HTTPS POST and nothing listening. Both sinks are
optional and both are attempted; either alone is a complete configuration.

| sink | file | what it produces |
|---|---|---|
| board | `scripts/notify-board.py` | a `critical` issue, assigned, that wakes the assignee |
| telegram | `scripts/notify.sh` | one chat message |

**One issue per outage, not one per alert.** The watchdog repeats hourly while
the stand is still down, so the board sink takes a `--key` naming the ongoing
condition: the first alert opens the issue, repeats comment on it, and recovery
closes it. Without that, a long outage would produce a `critical` issue every
hour and the board would be noisier than the silence it replaced. The watchdog
uses `stand-down`; the CI failure path uses `deploy-failed`, deliberately
separate so a failed deploy is not folded into an unrelated outage. The recorded
issue is re-checked before reuse, so an alert never lands in a thread someone
has already closed.

A successful deploy resolves `deploy-failed` on its way out, so a fixed deploy
clears its own alert. An alert channel that needs manual cleanup gets muted.

### Credentials

Both paths call `scripts/notify.sh`, which reads each sink's credentials from the
environment or from the deploy host's `~/audr/secrets/`. Nothing is in this
repository — it is public, and `secrets/` is gitignored.

| file | keys |
|---|---|
| `paperclip.env` | `PAPERCLIP_API_URL`, `PAPERCLIP_API_KEY`, `PAPERCLIP_COMPANY_ID`, optional `AUDR_ALERT_ASSIGNEE_AGENT_ID` / `AUDR_ALERT_PROJECT_ID` / `AUDR_ALERT_PRIORITY` |
| `telegram.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

`notify-board.py` parses `paperclip.env` rather than sourcing it, and takes only
the keys above — the file holds an API key, and sourcing it would execute
whatever else ended up in there.

**`notify.sh` with no credentials spools the message and exits 0** (see "Alerts
the host could not send" below — it is not lost). Exiting 0 is deliberate: every
caller is already on a failure path, and an alerting helper that turns "the
stand is down" into "the alerting helper also broke" is worse than none. The
consequence is that the code paths all run on a fresh clone, and that activating
a channel is purely dropping its file in place — no code change, no redeploy:

```bash
# 1. A BOARD USER mints the key: agent API key, scope {"kind":"task_bridge"},
#    scoped to the audr project with allowedAssigneeAgentIds limited to the
#    agent that should be woken. Agents cannot mint their own keys — the API
#    answers 403 "Board access required" — so this step needs a human, once.
#    Prefer task_bridge over a standard key: it can file and update issues and
#    nothing else, which is all a host-side alerter should be able to do.
# 2. On the deploy host:
umask 077
cat > ~/audr/secrets/paperclip.env <<'CREDS'
PAPERCLIP_API_URL=https://<paperclip-host>
PAPERCLIP_API_KEY=<task_bridge key>
PAPERCLIP_COMPANY_ID=<company uuid>
AUDR_ALERT_ASSIGNEE_AGENT_ID=<agent uuid to wake>
AUDR_ALERT_PROJECT_ID=<project uuid>
CREDS
# 3. Prove delivery. --strict makes an unconfigured/undelivered send exit 1.
#    This opens a REAL critical issue — close it afterwards.
~/.local/share/audr-watchdog/scripts/notify.sh --strict "audr alerting test"
```

Do not put a run-scoped agent token in that file. The `PAPERCLIP_API_KEY` an
agent gets at runtime is a JWT with a `run_id` claim and a 48-hour expiry; it
would work for one afternoon and then fail silently on the one day it mattered.

Telegram, if you also want it:

```bash
# @BotFather -> /newbot; send the bot a message; read the chat id from
# https://api.telegram.org/bot<TOKEN>/getUpdates  ->  result[].message.chat.id
umask 077
cat > ~/audr/secrets/telegram.env <<'CREDS'
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=987654321
CREDS
```

### Alerts the host could not send

Until `paperclip.env` is in place the deploy host can detect an outage but not
report one, and that gap cannot be closed from the host: minting a long-lived
`task_bridge` key needs a human board user, and the only token an agent could
copy there expires in 48 hours. So an alert no sink accepted is appended to a
spool file instead of being left in the journal to rotate away:

```bash
cat ~/.local/state/audr-notify/undelivered.jsonl   # one JSON record per alert
```

The spool is capped at the newest 200 records (`AUDR_SPOOL_MAX_LINES`), oldest
dropped first — during an outage the latest alert is the one a responder needs.

**Draining it is an agent's job, not the host's.** An agent run already carries
a board credential for the length of the run, so the replay happens there:

```bash
python3 scripts/drain-alert-spool.py --dry-run   # what would be sent
python3 scripts/drain-alert-spool.py             # send it, then clear the spool
```

This buys delivery at agent-heartbeat latency rather than within the ~5-minute
probe window. That is strictly worse than a credential on the host and much
better than nothing, and it needs nobody's permission to turn on. Once
`paperclip.env` lands the spool simply stops filling and the drain finds nothing
— there is no step to undo.

Two behaviours worth knowing before you read a replayed alert:

- **Repeats collapse.** The watchdog re-alerts hourly while the stand is down,
  so a spool drained a day later can hold a dozen records for one condition.
  Each `--key` is collapsed to its newest alert plus a trailing recovery if one
  came after it, so a finished outage reads as one issue opened and closed.
- **Replays are labelled.** Every drained alert is prefixed with when it was
  spooled and when it was delivered, because an unlabelled "🔴 audr stand DOWN"
  sends somebody to the host for an incident that ended yesterday.

A spool nobody drains is itself an alertable condition since AUD-595: the
watchdog measures the age of the oldest record here and speaks at 12h — see
[*When delivery only reaches the spool*](#when-delivery-only-reaches-the-spool-aud-595).
News alerts that took this road also say so in the database (`news_alert
.dispatched_via = 'spool'`), so "dispatched" in the app no longer implies
anybody was pushed at.

`--strict` sends are never spooled: that flag is a human connectivity probe, and
its test text would otherwise be replayed later as a real alert.

### Drilling an alert without publishing one

`--strict` proves delivery and therefore opens a real `critical` issue. To prove
the *condition logic* instead — does this threshold trip, does the body read
correctly, do the credentials resolve — use `--dry-run`, which renders the alert,
reports the dedupe decision it would have taken, and writes to no sink and no
spool:

```bash
scripts/notify.sh --dry-run --key backup-stale "drill"

# Whole watchdog path, including a backdated dump fixture, against scratch state:
D="$(mktemp -d)"; mkdir -p "$D/backups" "$D/state"
touch -d '2 days ago' "$D/backups/audr-20260101-000000.sql.gz.age"
AUDR_NOTIFY_DRY_RUN=1 AUDR_BACKUP_DIR="$D/backups" \
AUDR_WATCHDOG_STATE_DIR="$D/state" AUDR_NOTIFY_STATE_DIR="$D/state" \
  scripts/stand-watchdog.sh
```

Prefer the `AUDR_NOTIFY_DRY_RUN=1` environment spelling when the caller is a
script you are not editing — every nested `notify.sh` inherits it. Point the
state directories at scratch too: the watchdog writes its own dedupe files
either way, and a drill that leaves `backup-stale-alerted-at` behind suppresses
the next *real* alert for 24h.

**Why this exists.** AUD-570 was a false `critical` card reporting the stand's
backups as 27h stale, with a `/tmp` scratch path in the body and the wrong
hostname — a drill of the AUD-567 alert path, published because an agent run
carries live board credentials in its environment, so `notify.sh` had a working
board sink nobody meant to use. The stand's newest dump was three hours old at
the time. A drill that cannot be told apart from an incident costs more than it
proves.

### Installing and inspecting the watchdog

```bash
scripts/install-watchdog.sh            # install or update, then show status
scripts/install-watchdog.sh --check    # what is installed, and whether each sink answers
```

Both modes end by **probing** the sinks instead of listing their files
(AUD-594): the board credential through a read-only `notify-board.py --probe`
(`GET /api/agents/me`, never a POST), the Telegram token through `getMe`. Read
that verdict, not the file list above it:

| line | means |
|---|---|
| `Alerts will be delivered — probed sink(s): …` | those sinks answered; delivery is direct, inside the probe window |
| `configured and did NOT answer: …` | the file is there and the channel is dead — delivery falls back to the spool, at agent-heartbeat latency |
| `NO CHANNEL CONFIGURED` | no credential file at all |
| `the installed notify-board.py predates --probe` | the host copy is stale; re-run the installer |

Why this is worth a probe: until AUD-594 the installer printed
`Alert credentials present — alerts will be delivered.` whenever
`secrets/paperclip.env` existed. On the stand it existed **and the key inside had
expired**, so every alert spent a 401 and then spooled. Nothing was lost — the
spool is the designed fallback — but the one line summarising the whole install
promised the fast path while the slow one was in use. A 401 here is not an
incident; believing that message was the problem.

A 403 is reported as live on purpose: the credential this file should hold is a
`task_bridge` key scoped to opening one agent's issues, and such a key can be
refused on `/api/agents/me` while being perfectly able to POST the alert. An
expired or wrong key answers 401.

The installer copies the four scripts to `~/.local/share/audr-watchdog/scripts/`
and renders both units from `ci/host-units/audr-watchdog.service.in` and
`ci/host-units/audr-alert-dispatch.service.in` (AUD-584 — see
[news alert dispatch](#news-alert-dispatch-adr-007-phase-2) below). The copy is
the point: the only git checkout on that host belongs to the CI runner, which
rewrites it every deploy and resets it on a failed one, so a watchdog pointed at
it would lose its code exactly when it is needed.

**Editing `scripts/stand-watchdog.sh`, `scripts/alert-dispatch.sh`,
`scripts/notify.sh` or `scripts/notify-board.py` does not reach the host until
`install-watchdog.sh` runs again.** This is the opposite of the workflows, which ship with the commit
since AUD-443 — remember the difference. (The CI failure path runs from the
runner's checkout, so it does get the new code with the commit; only the
watchdog's copies need the installer.)

```bash
systemctl list-timers audr-watchdog.timer      # next/last probe
sudo systemctl start audr-watchdog.service     # probe right now
cat ~/.local/state/audr-watchdog/consecutive-failures   # 0 when healthy
ls ~/.local/state/audr-watchdog/backup-stale-alerted-at # absent = backups fresh
ls ~/.local/state/audr-notify/                 # issue-<key>: open alert issues
                                               # undelivered.jsonl: spooled alerts

# Unit tests for the board sink's dedupe/lifecycle logic (no network):
python3 scripts/test_notify_board.py
# ...and for the spool drain's collapse logic (no SSH, no board):
python3 scripts/test_drain_alert_spool.py

# Probe history and alert decisions. `sudo` is required: the unit runs as
# codex but logs to the system journal, and plain `journalctl -u` as codex
# prints "No entries" rather than an error, which reads exactly like a dead
# timer. One healthy tick logs two lines: the backup-freshness verdict
# ("[watchdog] newest backup is Nh old") and then "[watchdog] ok".
sudo journalctl -u audr-watchdog.service -n 50
```

To silence it during planned maintenance, `sudo systemctl stop
audr-watchdog.timer` — and start it again afterwards. Stopping the timer is
better than raising the threshold, because the counter file resets on the first
healthy probe either way. Note that stopping this timer also stops the
backup-freshness check, so a maintenance window long enough to span a missed
03:20 would go unreported — start it again the same day.

## News alert dispatch (ADR-007 phase 2)

The third timer on this host, `audr-alert-dispatch.timer`, installed by the same
`scripts/install-watchdog.sh` and running `scripts/alert-dispatch.sh` once a
minute. It is the bridge between the stack and a channel: `news_analyze` writes
`critical` rows into `news_alert` inside the stack, and this script pushes them
through the same `notify.sh` the watchdog uses.

```
docker compose exec -T worker python -m audr.jobs.alerts_pending   # JSON, pending only
scripts/notify.sh --key news-<asset>-<category> -- <text>          # board + telegram
docker compose exec -T worker python -m audr.jobs.alerts_mark <id> # dispatched_at = now()
```

Four things about it are deliberate and worth knowing before you debug it:

- **`dispatched_at` means notify.sh accepted the message (exit 0)**, not that
  anyone read it. Everything after that point is notify.sh's: its own Telegram
  retries, and the spool that `drain-alert-spool.py` replays. Do not add retries
  on top — two schedules over one message is how duplicates become unexplainable.
- **At-least-once.** If the tick dies between a successful send and the mark, the
  next tick re-sends. `--key` makes the board sink comment on the issue already
  open instead of filing a second one, which is the whole reason that trade is
  acceptable (ADR-007 §1).
- **Nothing is ever `--resolve`d.** A news story has no "incident over" moment to
  detect, so the alert issue is closed by the assignee it woke. The follow-up
  wave of articles is collapsed upstream by `news_analyze`'s 24h cooldown per
  `(asset, category)` and here by `--key`.
- **Only `critical` is pushed.** `warning` accumulates in the in-app list on
  purpose; a channel that reports every headline gets muted, and a muted channel
  is worse than none.

```bash
systemctl list-timers audr-alert-dispatch.timer   # next/last tick
sudo systemctl start audr-alert-dispatch.service  # dispatch right now
sudo journalctl -u audr-alert-dispatch.service -n 50
# A quiet tick logs exactly one line, on purpose — a dead timer and a quiet news
# day must not look identical:
#   [alert-dispatch] no pending critical alerts

# Drill the whole path, delivering nothing and marking nothing. The last line
# names the sinks that would have taken it — `board=ok` is what you want:
~/.local/share/audr-watchdog/scripts/alert-dispatch.sh --dry-run

# If a drill reports `no channel configured` / `reason would have been
# unconfigured` while ~/audr/secrets/paperclip.env exists, the sinks are fine and
# AUDR_REMOTE_DIR simply is not reaching notify.sh — that is how it finds
# secrets/. alert-dispatch.sh exports its own default for exactly this reason,
# but a bare notify.sh run inherits nothing, so compare the two before
# concluding the board sink is broken (this cost two runs an hour on AUD-584):
env -u AUDR_REMOTE_DIR ~/.local/share/audr-watchdog/scripts/notify.sh \
  --dry-run --key probe -- probe      # -> no channel configured
AUDR_REMOTE_DIR=~/audr ~/.local/share/audr-watchdog/scripts/notify.sh \
  --dry-run --key probe -- probe      # -> board=ok

# `board=ok` means the credential RESOLVED and ANSWERED since AUD-594: a dry run
# now probes it (read-only) instead of reporting a parsed file as a live sink.
# A dry run against an expired key says so and still exits 0 — a drill must not
# render as a real delivery failure:
#   [notify-board] PROBE — credential REJECTED at https://... (HTTP 401): ...
#   [notify-board] DRY RUN — the board sink would NOT have accepted this alert

# What is pending and why a previous attempt failed (dispatch_error):
docker compose exec -T db psql -U audr -d audr -c \
  "SELECT id, severity, category, created_at, dispatched_at, dispatch_error
     FROM news_alert ORDER BY created_at DESC LIMIT 10"
```

Tests run in CI, against the shipped script with a stubbed `docker` and
notify.sh: `backend/tests/unit/test_alert_dispatch.py` (delivery/marking
handshake, hostile headlines, a down stack) and
`backend/tests/unit/test_alerts_cli.py` (the two CLI points' contract).

If the stack is down, this script says so in one line and exits 0 — the outage
itself is `audr-watchdog`'s alert to send, and a unit failing every 60s would
only repeat it. Alerts are not lost meanwhile: they stay pending in the database
and the tick after recovery takes them.

### When the dispatcher itself goes mute (AUD-592)

**Do not read `systemctl status audr-alert-dispatch` as an answer.** That exit 0
above is unconditional: on any unreadable stack the script prints `cannot read
pending alerts (exit N) — nothing dispatched` and succeeds, so a bridge that can
deliver nothing at all still leaves `ExecMainStatus=0` behind it. That state was
real for an hour after phase 2 merged — the timer ticked against an image with
no `audr.jobs.alerts_pending`, four runs, zero possible deliveries — and it was
found only by reading the journal by eye.

`audr-watchdog` now has the voice for it. `check_alert_dispatch_lag` queries
`news_alert` **directly through `psql` in the db container**, which shares no
code with the dispatcher, so it still sees the rows when the dispatcher's own
half is the broken part. It alerts under `--key alert-dispatch-stale` when
either:

- a `critical` row has been pending more than 15 minutes
  (`AUDR_DISPATCH_LAG_MINUTES`) — nothing is even attempting delivery; or
- any pending `critical` row carries a `dispatch_error` — notify.sh was reached
  and refused. No grace on this one, deliberately: notify.sh exits 0 for every
  outcome it handles (including spooling), so a non-zero exit means it could not
  run at all.

One key for both, because they want the same first action and an errored row is
usually also a stale one. It `--resolve`s when the condition clears, reminds
every 6h while it lasts (`AUDR_DISPATCH_REMIND_HOURS`), and stays silent — with
a named journal line — when the query cannot run at all or when migration 0033
has not reached this stand. An unreadable database is not evidence of a stalled
dispatcher, and both of those causes already have owners.

```bash
# What the check sees, by hand (this is the condition it queries):
cd ~/audr && docker compose exec -T db psql -U audr -d audr -tA -F '|' -c \
  "SELECT count(*) FILTER (WHERE created_at < now() - interval '15 min'),
          count(*) FILTER (WHERE dispatch_error IS NOT NULL)
     FROM news_alert WHERE dispatched_at IS NULL AND severity = 'critical'"

sudo journalctl -u audr-watchdog -n 20 | grep dispatch
#   [watchdog] alert dispatch ok (no critical alert pending over 15m)    healthy
#   [watchdog] ALERT sent — alert dispatch stale (stale=2 errored=0 ...)  firing
#   [watchdog] cannot read news_alert (exit 1) — dispatch check skipped   quiet, by design
```

**Drilling it safely.** Preparing an overdue row means putting a row the
*dispatcher* will also pick up and deliver into the live database, so a drill
has to disarm delivery first — and `systemctl stop audr-alert-dispatch.timer`
is not enough. Stopping a timer does not kill a service instance already
mid-tick, and a tick takes several seconds. That is how the AUD-592 drill
itself ended up with its own `AUD-592 DRILL ROW` text spooled under key
`news-crv-other`, one `drain-alert-spool.py` run away from being published as a
real `critical` board issue:

```bash
# BOTH, timer and service — for both units:
sudo systemctl stop audr-alert-dispatch.timer audr-alert-dispatch.service \
                    audr-watchdog.timer audr-watchdog.service
# ... insert the row; run the watchdog by hand with AUDR_NOTIFY_DRY_RUN=1 ...
# ... delete the row; restart both timers ...

# Then ALWAYS check what the drill left behind. A dry-run watchdog does not
# spool, but a live dispatcher tick that raced it does:
grep -c DRILL ~/.local/state/audr-notify/undelivered.jsonl \
  && : > ~/.local/state/audr-notify/undelivered.jsonl
```

`AUDR_NOTIFY_DRY_RUN=1` covers the hand-run watchdog only. It cannot cover a
systemd tick, and an agent run's live `PAPERCLIP_*` environment gives notify.sh
a working board sink from any directory — which is how the false `critical`
AUD-570 got published.

The watchdog runs the query on stdin rather than `psql -c`, and that is
load-bearing: psql interpolates `:'var'` in its own lexer, so with `-c` the
server gets the colon unparsed and every tick dies on `syntax error at or near
":"` — which this check would then report as "cannot read". Do not simplify it
back.

**This check cannot see the spool road, by design.** Because notify.sh exits 0
when it only *spools* an alert, the dispatcher marks that row dispatched and it
leaves the pending set — so the other AUD-584 road ("delivered to nobody, and
the spool only moves when an agent drains it") is invisible here, and has to
stay invisible here. That road has its own artefact and its own check: next
section.

### When delivery only reaches the spool (AUD-595)

`dispatched_at` means "notify.sh accepted it" (ADR-007 rev 2.1), and notify.sh
accepts an alert **no sink would take**: it writes it to
`~/.local/state/audr-notify/undelivered.jsonl` and exits 0, because that spool
is what `drain-alert-spool.py` replays. So one `dispatched_at` covered two very
different outcomes — somebody was pushed at, or nobody was — and nothing in the
database, the in-app list or the watchdog could tell them apart. On this stand
that is the *common* case, not a hypothetical: the board token answers 401, so
every critical news alert currently takes the spool road.

**The decision.** Three things, and deliberately not a fourth:

1. **notify.sh reports which road it took**, in a file named by `--road-file`:
   one word — `board` / `telegram` / `board+telegram` / `spool` / `none` /
   `dry-run`. A file rather than the exit code, because five callers already
   read that exit status and redefining `0` for all of them to tell the
   dispatcher one thing would trade a silent gap for a blast radius.
2. **`news_alert.dispatched_via` records it** — migration 0034, written by
   `alerts_mark --via`, exposed on `GET /api/v1/alerts`. `dispatched_via =
   'spool'` reads as "out of the dispatcher's queue, nobody pushed yet".
3. **The spool's own age is the watched artefact** — `check_alert_spool_age` in
   `stand-watchdog.sh`, `--key alert-spool-stale`, firing when the oldest record
   is over 12h old (`AUDR_SPOOL_MAX_AGE_HOURS`, against a drain routine on a
   ~3h cadence). The same shape as AUD-567's backup check, which watches the
   dump's mtime rather than the timer that writes it. It counts
   `undelivered.jsonl.draining` as well as `undelivered.jsonl`: a drain claims
   the spool by moving it aside, so an interrupted one strands undelivered
   records in the claim file, and reading only the live spool would call that
   state healthy.

**What was rejected, and why it matters.** The one-line fix is to pass
`--strict` so a spool becomes a non-zero exit, a `dispatch_error`, and a row
that stays pending for the lag check above to find. Against the current 401
board token that would make *every* dispatch fail, *every* row stay pending and
the lag check fire continuously — a muted channel instead of a silent gap — and
it would disable the spool on the one path that delivers anything at all. So a
spooled row still leaves the pending set and the lag check stays quiet;
`dispatched_via` carries the truth and the spool's age carries the alarm.

The spool alert itself travels by the spool when there is no working sink, so
you will usually read it as a replay from the very drain it is asking for. Two
consequences: records under its own key are excluded from the age it measures
(otherwise its first message would make the condition permanently true), and
what it ultimately asks for is a working board credential, not a drain.

```bash
# Which alerts were accepted but never pushed at anybody:
cd ~/audr && docker compose exec -T db psql -U audr -d audr -c \
  "SELECT id, severity, created_at, dispatched_at, dispatched_via
     FROM news_alert WHERE dispatched_via = 'spool' ORDER BY created_at DESC LIMIT 10"

# What the watchdog sees, and the three states it distinguishes:
sudo journalctl -u audr-watchdog -n 20 | grep spool
#   [watchdog] alert spool ok (no spool file at ...)                     healthy
#   [watchdog] alert spool ok (1 record(s), oldest 1h, limit 12h)        in flight
#   [watchdog] ALERT sent — alert spool stale (2 record(s), oldest 20h)  firing

python3 scripts/drain-alert-spool.py --dry-run   # from an agent run: what would be sent
```

`dispatched_via` is NULL on rows dispatched before migration 0034 — honestly
"road unknown" rather than a backfilled guess — and on any row marked by a
dispatcher whose `notify.sh` predates `--road-file`. `install-watchdog.sh` ships
both files together, so that pairing only comes apart if someone copies one by
hand.

Drilling it is cheap and needs no database: append a record with an old `ts` to
a scratch spool and point the watchdog at it. Use a scratch state directory for
both halves so a drill cannot stand down a real alert, and `--dry-run` so the
message is not published (`AUDR_NOTIFY_DRY_RUN=1` is inherited by the nested
notify.sh — but it cannot cover a *systemd* tick, so stop the timer **and** the
service exactly as in the section above):

```bash
D=$(mktemp -d); mkdir -p "$D/state"
python3 - "$D/state/undelivered.jsonl" <<'PY'
import json, sys, time
old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 20 * 3600))
open(sys.argv[1], "w").write(json.dumps(
    {"ts": old, "key": "news-eth-hack-exploit", "resolve": False,
     "reason": "unconfigured", "message": "DRILL ROW"}) + "\n")
PY
AUDR_NOTIFY_DRY_RUN=1 AUDR_NOTIFY_STATE_DIR="$D/state" \
AUDR_WATCHDOG_STATE_DIR="$D/wd" \
  ~/.local/share/audr-watchdog/scripts/stand-watchdog.sh | grep spool
rm -rf "$D"
```

## Installing and inspecting the scheduled backup

The second timer on this host, installed the same way and for the same reason
(pinned copies, not the runner's checkout). Full operator documentation is in
[`operations.md#backups`](operations.md#backups); this is the host-side view.

```bash
scripts/install-backup.sh            # install or update, take a first dump, show status
scripts/install-backup.sh --check    # units, timer, dumps on disk, engine, KEK presence
```

```bash
systemctl list-timers audr-backup.timer        # next/last dump (03:20 UTC daily)
sudo systemctl start audr-backup.service       # dump right now
ls -la ~/audr/backups/                         # the dumps themselves, mode 600
sudo journalctl -u audr-backup.service -n 50   # per-run log: dump, decrypt check, prune
```

Each run takes a dump, **decrypts and decompresses its own output** to prove the
archive is readable, prunes to `--days 7 --weeks 4`, and alerts through the same
`notify.sh` sinks as the watchdog on any failure — with a flag file at
`~/audr/backups/.last-run-failed` so the next success stands the alert down.
A backup that quietly stops being taken was the actual AUD-562 gap, so it is
itself an alertable condition rather than something you notice during a
restore.

Two things worth knowing before you touch it:

- **The dumps are on the same host as the database.** They cover volume loss, a
  bad migration and an accidental `down -v`; they do **not** cover loss of the
  host, and the schedule deliberately writes no KEK copy beside them. Getting a
  dump and the `SECRET_KEY` off-host is an owner action — see
  [operations.md](operations.md#what-the-schedule-does-and-does-not-protect-against).
- **`--weeks 4` is load-bearing.** A dump is 49 MiB since AUD-564 gzips it
  before encrypting (282 MiB before that); without the cap the weekly tier
  still grows forever, now ~2.5 GiB/year rather than ~14.7. Compression bought
  headroom, not an exemption from bounding a directory a timer writes to.

As with the watchdog, editing `backup.sh`, `backup-run.sh` or `dump-prune.py`
in this repo does not reach the host until `install-backup.sh` runs again.

## Recovering a schema/image mismatch by hand

Symptom: `migrate` crash-loops with `Can't locate revision identified by 'NNNN'`,
`api`/`worker` stuck in `Created`, and port 80 refuses connections (there is no
proxy container left to answer with a 502 — the thing that publishes :80 *is*
the `api` container).

```bash
# 1. If a deploy job is wedged, kill it — a blocked `compose up` holds the
#    audr-deploy concurrency group and starves every queued build.
ps -eo pid,etime,cmd | grep -E 'workflow/[0-9]+\.sh|docker compose'

# 2. Find where the DB actually is, and which local image knows that revision.
docker exec audr-db-1 psql -U audr -d audr -tAc 'select version_num from alembic_version'
docker run --rm --entrypoint sh <candidate-image> -c 'ls /app/migrations/versions'

# 3. Either roll the schema back to match the image that is meant to run…
set -a; . ~/audr/.env; set +a
docker run --rm --network audr_internal -w /app \
  -e DATABASE_URL="postgresql+psycopg://audr:${DB_PASSWORD}@db:5432/audr" \
  "$AUDR_REGISTRY/audr-backend:<image-with-the-newer-revisions>" \
  python -m alembic downgrade <target-rev>
# …or roll forward by pinning BACKEND_TAG to an image whose migration tree
# contains the DB's revision. Check the downgrade is safe first (a table the
# downgrade drops must be empty).

# 4. Bring the stack back and verify with the same four signals as the gate.
cd ~/audr && timeout --foreground 300 docker compose up -d --remove-orphans
curl -s http://localhost/health/ready   # must contain "status":"ok"
curl -s -o /dev/null -w '%{http_code}\n' http://localhost/
curl -s http://localhost/api/v1/version # version + commit actually running
```

After a manual recovery the live version is deliberately *not* the one this
checkout carries, so pin the expectation when smoke-testing it:

```bash
AUDR_EXPECT_VERSION=<the version you rolled back to> ./scripts/smoke-test.sh
```

See `docs/releases.md` for the versioning and image-tagging scheme.

`GET /health/ready` is the trustworthy public signal, because it performs a real
DB and master-key check inside FastAPI rather than merely proving something is
listening. Since AUD-388 a 200 on `/` is also meaningful — the SPA is served by
the `api` container, so it cannot answer at all while the API is down — but it
still says nothing about the database, which is the usual thing broken here.

## Deploy host disk retention

The host runner is also the deploy host, so every push to `main` mints a new
~500MB `audr-backend:<sha>` image on the same 32GB root filesystem that carries
the live stack's database volume. Nothing removed them until AUD-395: by
2026-10-03 the host held 299 images (14.9GB) plus 3.8GB of build cache at 89%
full — about four more deploys before a build would have hit ENOSPC, taking the
database down with it rather than failing politely.

`scripts/host-gc.sh` now runs as a `if: always()` step at the end of both
`deploy.yaml` and `build.yaml`. It prunes build cache and unreferenced images
older than 48h, caps how many unused release images may stay resident
regardless of age, escalates to much shorter windows when the root filesystem
is still ≥80% full afterwards, and always exits 0 — a GC problem must never
fail an otherwise healthy deploy. Confirm it ran by looking for
`[host-gc] after: ...` in the job log.

```bash
# ad-hoc run: copy it over, since the runner's checkout dir is not stable
scp -i id_ed25519 scripts/host-gc.sh "$AUDR_DEPLOY_HOST:/tmp/"
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'bash /tmp/host-gc.sh --dry-run'   # report only
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'IMAGE_MAX_AGE=24h CACHE_MAX_AGE=24h bash /tmp/host-gc.sh'
```

### Age alone is rate-blind — the count cap and the escalation

An age window retains "two days of deploys", which is a handful of images on a
quiet day and well over a dozen on a busy one. On 2026-10-04 (AUD-425) the host
was back at 80% with ~20 release images (~10GB) *all younger than the 48h
window*: GC ran on every merge, found nothing eligible, and logged its own warn
threshold rather than acting on it — the AUD-379 failure mode from the other
side. Two additions make retention pressure-aware without tightening the
relaxed default:

* **`IMAGE_KEEP_COUNT`** (default 5) — at most this many *unused* `audr-*`
  images stay resident, newest first, however young. Images a container
  references and whatever `:latest` and `:rollback` point at are excluded from
  the cap entirely, so the live stack and the no-pull rollback are never the
  thing trimmed. Removal uses `docker image rm` without `-f`, so a referenced
  image is refused rather than yanked even if that protected set were wrong.
* **Automatic escalation** — if `/` is still ≥ `WARN_PCT` after the normal
  pass, the script immediately repeats it with `ESCALATE_CACHE_MAX_AGE` /
  `ESCALATE_IMAGE_MAX_AGE` (6h) and `ESCALATE_IMAGE_KEEP_COUNT` (2), logging
  `escalating: ...` so the job log says it happened. If the filesystem is
  *still* over the line after that, Docker is no longer what is filling the
  disk, and the second warning says so.

Pruning this host cannot disarm rollback. `deploy.yaml`'s snapshot step pushes
`:rollback` to the registry precisely so the rollback path does not depend on
the host's local cache surviving a prune, and `restore_image()` recovers in
three steps: local, then the immutable per-sha tag in the registry, then
`:rollback`. Host-local images are a cache, not the source of truth. The 48h
window is belt-and-braces on top of that, keeping the previous release resident
so the common rollback needs no pull at all; `docker image prune` never
considers an image a running container references.

### CI test images are the other accumulator

Release images are not the only thing that grows. `ci.yaml` namespaces its
compose project per run (`audr-test-ci-<run>-<attempt>`, from AUD-310, so
concurrent runs cannot destroy each other's database), which means the images
compose builds are named per run too — about 1GB of `-backend-tests` plus
`-migrate-test` on every CI run. A unique name can never be a cache hit for a
later run, so they are garbage the moment the job ends, yet `docker compose
down -v` removes containers, volumes and networks but *not* images. Under the
48h release window alone they outlived their usefulness by two days: the first
measurement after the GC step shipped found 27 such images from 16 past runs
still resident, and CI pushed the host from 44% to 60% in a couple of hours.

So they are deleted at the source: `ci.yaml`'s teardown step removes the images
matching its own `$COMPOSE_PROJECT-*` — scoped to that run, so a concurrent CI
job on the other runner slot is untouched. `host-gc.sh` then reaps any
`audr-test-ci-*` image older than `TEST_IMAGE_MAX_AGE_HOURS` (default 6) as a
second line of defence, for the case where the runner was killed before its
teardown step could run. That reaper uses `docker image rm` *without* `-f`, so
an image a container still references is refused rather than pulled out from
under a live run.

### Disk pressure

Everything above is the *mechanism*. Until AUD-580 none of it had a **voice**:
`host-gc.sh`'s `WARN: root filesystem still at N% after GC` and the much worse
`WARN: Docker is no longer the thing filling this disk` only ever `echo`, from a
step inside a CI job. That output lands in a Gitea job log, and Act Runner's
journal carries only its own meta lines — `journalctl -u act_runner | grep
host-gc` returns nothing even for runs that pruned successfully. Nobody reads a
job log on a green deploy, so the most serious condition the GC could detect was
structurally invisible on a host where exhaustion takes `db_data` with it.

`scripts/stand-watchdog.sh` now carries a `check_disk_space` alongside its
backup-freshness check, for the same reason: both watch a slow condition, and
giving either its own timer would need that timer's own liveness proof,
recursively (the AUD-567 trap). The watchdog is observably firing every 60s.

It alerts at **85%**, deliberately above host-gc's `WARN_PCT=80`. 80% is where
host-gc escalates to 6h windows and usually recovers the space by itself, so
alerting there would fire on every busy merge day and train the channel to be
ignored. 85% means escalation has already run and not recovered — which is the
actionable state. Alert key `disk-pressure`, so it is its own board thread, with
a `--resolve` stand-down once the disk drops back below the threshold, and one
reminder every 6h while it persists.

When it fires, the first question is whether Docker is still the thing growing.
The alert body carries `df -h /` and the `docker system df` breakdown so that is
answerable without an SSH session:

```bash
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'bash /tmp/host-gc.sh --dry-run'
ssh -i id_ed25519 "$AUDR_DEPLOY_HOST" 'sudo du -xh --max-depth=1 / | sort -rh | head'
```

A large **non-Docker** directory means retention is not the problem and tighter
GC windows will not help. Note that `/var/lib/containerd` is Docker's own
content store on this host, not a second runtime, so it is expected to hold
roughly what `docker system df` reports — do not count it twice.

A high reading is **not by itself** a GC failure. Build cache made purely of
sub-48h entries is the window working as designed: on 2026-10-16 `/` moved
63% → 75% in a day on merge rate alone (7 deploys in 11h, each leaving ~2×278MB
of non-shared `npm ci` / `uv sync` exec-mount cache), while the image side sat
provably at its `IMAGE_KEEP_COUNT=5` floor. Confirm the cap before touching any
window — and re-read `host-gc.sh`'s header, which warns against exactly that.

Override the thresholds per-host with `AUDR_DISK_WARN_PCT`, `AUDR_DISK_PATH` and
`AUDR_DISK_REMIND_HOURS`; drill it without publishing via the `--dry-run` path in
"Drilling an alert without publishing one" above.

## Registry tag retention

The registry (`$AUDR_REGISTRY`) is a plain CNCF `distribution` registry
behind nginx — **not Harbor**, despite the name used in older notes. It exposes
only the `/v2` API (`/api/v2.0/systeminfo` 404s), so there is no retention
feature to switch on: retention is enforced from outside by
`scripts/registry-prune.py`, which needs nothing but HTTP.

Since AUD-345 that script is **not something you have to remember to run**:
`deploy.yaml`'s `Enforce registry tag retention` step runs it with `--apply` at
the end of every *successful* deploy, on the deploy host, so the default
`--env-file` is the live `~/audr/.env` and the deployed tag is protected from
the inside. Two deliberate asymmetries with the `host-gc.sh` step next to it:

- it is **not** `if: always()`. After a failed deploy the stack has just been
  rolled back and `:rollback` is the only thing between us and an outage — the
  worst possible moment to delete from the registry, and no bytes would be
  freed anyway.
- it exits 0 even when the pruner fails. Retention is hygiene on a healthy
  deploy and must never turn a green deploy red; look for
  `[registry-prune] FAILED` in the job log.

Run it by hand for a dry run, a one-off `--keep`, or the orphaned
`audr-frontend` repository:

```bash
python3 scripts/registry-prune.py                 # dry run; prints keep/prune per tag
python3 scripts/registry-prune.py --apply         # delete
python3 scripts/registry-prune.py --keep 12 --protect b57f2d107ecc
python3 scripts/test_registry_prune.py            # retention-rule unit tests, no deps
```

A tag survives if it is `latest`/`rollback`, looks like a release tag (`v0.1.0`),
is the tag pinned in `~/audr/.env`, is among the `--keep` newest by image
creation time, or shares a manifest digest with anything protected by those
rules. Three of those deserve explaining, because each one is load-bearing:

- **Deletion is by digest, and a digest delete removes every tag pointing at
  it.** Pruning a stale sha tag that happens to share `latest`'s or
  `rollback`'s digest would destroy the rollback path, so digests shared with a
  protected tag are excluded. (`f4c1636` survives today purely because it is
  `v0.1.0`'s digest.)
- **The live and previous sha tags are not history.** `deploy.yaml`'s
  `rollback()` restores the previous release by `docker pull`ing the immutable
  `audr-backend:<prev-sha>` from this registry, falling back to `:rollback`. A
  host prune or rebuild is exactly when that pull matters.
- **A sha must be kept in both repositories or neither.** Moot since AUD-388
  left `audr-backend` as the only pruned repository, but still what makes any
  multi-repo run safe. Back when a deploy pinned `BACKEND_TAG` and
  `FRONTEND_TAG` to the same sha, keeping it on one side gave a rollback that
  half-succeeded. Per-repo age ranking does not deliver symmetry by itself:
  many tags here share an identical image `created` timestamp (a rebuild of
  unchanged layers reuses the date) and the resulting ties break differently
  per repository. The script therefore unions each repository's newest-N and
  applies that union everywhere. The orphaned `audr-frontend` repository is no
  longer pruned by default; clear it deliberately with
  `--repo audr-frontend` if you want the catalog tidy.

The script refuses any repository not named `audr-*`: this registry is shared
with an unrelated project (`svetu-backend`, `svetu-frontend`, 91 tags each).

**Deleting tags does not free disk.** A manifest delete only unlinks; the blobs
are reclaimed solely by

```bash
registry garbage-collect -c /etc/docker/registry/config.yml   # on the registry host
```

which must run as a process on the registry host. We have no shell there,
so that half stays open and is tracked separately — until it
runs, pruning buys catalog clarity, not bytes. The same gap is why
`library/aud-pushtest` still appears in `/v2/_catalog` with `tags: null`.
