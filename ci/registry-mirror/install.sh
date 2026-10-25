#!/usr/bin/env bash
# Install/point a host at the LAN Docker Hub pull-through cache (AUD-747).
#
#   ./install.sh                 # serve the cache here *and* use it
#   ./install.sh --client-only   # only use a cache someone else serves
#   ./install.sh --check         # report state, change nothing
#
# Idempotent: safe to re-run, and re-running is how you verify. Needs sudo for
# /etc/docker/daemon.json and the ufw rule.
#
# Why a script and not a runbook: the client half is three edits to a JSON file
# that must stay in step across three hosts (build box, deploy host, Gitea
# runner), and a hand-edited daemon.json that drops `insecure-registries` takes
# the private registry at 192.168.1.90:8085 down with it.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# The host serving the cache. Everything else points here.
MIRROR_HOST="${MIRROR_HOST:-192.168.1.152}"
MIRROR_PORT="${MIRROR_PORT:-5001}"
MIRROR_URL="http://${MIRROR_HOST}:${MIRROR_PORT}"
LAN="${LAN:-192.168.1.0/24}"
DAEMON_JSON=/etc/docker/daemon.json

MODE=server
for arg in "$@"; do
  case "${arg}" in
    --client-only) MODE=client ;;
    --check)       MODE=check  ;;
    *) echo "Unknown option: ${arg}" >&2; exit 1 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }

# --- daemon.json ------------------------------------------------------------
# Merged in Python rather than with jq (not installed everywhere) and rather
# than by rewriting the file wholesale: the existing keys — live-restore, the
# log-driver caps, and above all the 192.168.1.90:8085 entry in
# insecure-registries — must survive untouched.
#
# `registry-mirrors` only affects docker.io, and dockerd falls back to the
# upstream when the mirror does not answer, so a dead cache degrades to today's
# behaviour instead of breaking pulls.
#
# The mirror is plain HTTP, which dockerd refuses unless the same host:port is
# also in insecure-registries.
merge_daemon_json() {
  local mode="$1" # apply | check
  sudo -n python3 - "${DAEMON_JSON}" "${MIRROR_URL}" "${MIRROR_HOST}:${MIRROR_PORT}" "${mode}" <<'PY'
import json, os, sys, tempfile

path, mirror_url, mirror_hostport, mode = sys.argv[1:5]

cfg = {}
if os.path.exists(path):
    with open(path) as fh:
        text = fh.read().strip()
    if text:
        cfg = json.loads(text)

mirrors = list(cfg.get("registry-mirrors", []))
insecure = list(cfg.get("insecure-registries", []))
changed = False

if mirror_url not in mirrors:
    mirrors.insert(0, mirror_url)
    changed = True
if mirror_hostport not in insecure:
    insecure.append(mirror_hostport)
    changed = True

if mode == "check":
    print("CHANGED" if changed else "CURRENT")
    print(json.dumps({"registry-mirrors": mirrors, "insecure-registries": insecure}))
    sys.exit(0)

if not changed:
    print("CURRENT")
    sys.exit(0)

cfg["registry-mirrors"] = mirrors
cfg["insecure-registries"] = insecure

# Backup, then atomic replace: a half-written daemon.json stops dockerd from
# ever starting again, and this host runs other teams' stacks.
if os.path.exists(path):
    with open(path) as src, open(path + ".bak", "w") as dst:
        dst.write(src.read())

fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
print("CHANGED")
PY
}

if [[ "${MODE}" == check ]]; then
  say "mirror URL:    ${MIRROR_URL}"
  say "daemon.json:   $(merge_daemon_json check | head -1)"
  say "daemon sees:   $(docker info -f '{{json .RegistryConfig.Mirrors}}' 2>/dev/null || echo '?')"
  say "cache answers: $(curl -s -m 5 -o /dev/null -w '%{http_code}' "${MIRROR_URL}/v2/" || echo unreachable)"
  exit 0
fi

if [[ "${MODE}" == server ]]; then
  say "Starting the pull-through cache on :${MIRROR_PORT}"
  MIRROR_PORT="${MIRROR_PORT}" docker compose -f "${HERE}/compose.yaml" up -d

  if command -v ufw >/dev/null && sudo -n ufw status | grep -q '^Status: active'; then
    # Restricted to the LAN: an open pull-through cache is an open proxy for
    # anyone who can reach the port.
    say "Allowing ${LAN} to reach ${MIRROR_PORT}/tcp"
    sudo -n ufw allow from "${LAN}" to any port "${MIRROR_PORT}" proto tcp \
      comment "audr docker hub pull-through cache (AUD-747)" >/dev/null
  fi

  say "Waiting for the cache to answer /v2/"
  for _ in $(seq 30); do
    [[ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "${MIRROR_URL}/v2/" || true)" == 200 ]] && break
    sleep 1
  done
  code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "${MIRROR_URL}/v2/" || echo unreachable)"
  [[ "${code}" == 200 ]] || { echo "Cache did not come up (${MIRROR_URL}/v2/ -> ${code})" >&2; exit 1; }
  say "Cache up at ${MIRROR_URL}"
fi

say "Pointing this host's daemon at ${MIRROR_URL}"
result="$(merge_daemon_json apply | tail -1)"
if [[ "${result}" == CHANGED ]]; then
  # SIGHUP, not restart: registry-mirrors and insecure-registries are both in
  # dockerd's reloadable set, so running containers — other teams' stacks on
  # the build box, the live app on the deploy host — are left alone.
  say "Reloading dockerd (SIGHUP; running containers untouched)"
  sudo -n systemctl reload docker
else
  say "daemon.json already pointed at the cache"
fi

say "Daemon mirrors: $(docker info -f '{{json .RegistryConfig.Mirrors}}')"
say "Done."
