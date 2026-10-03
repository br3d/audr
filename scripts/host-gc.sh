#!/usr/bin/env bash
# Bound the deploy host's Docker disk usage.
#
# Why this exists (AUD-395): every push to main runs deploy.yaml, which builds
# and tags a new `audr-backend:<sha>` image on the host runner. Nothing ever
# removed them. By 2026-10-03 the host at 192.168.1.228 had accumulated 299
# images (14.9GB) plus 3.8GB of build cache and sat at 89% of a 32GB root —
# roughly 3.5GB free, or about four more deploys before a build would fail on
# ENOSPC. Disk exhaustion on this host does not fail politely: it takes the
# live stack's database volume down with it.
#
# Why pruning the host is safe. deploy.yaml's rollback path deliberately does
# NOT depend on the host's local image cache. The snapshot step pushes
# `:rollback` to Harbor precisely so that "a host-local tag is lost to
# `docker image prune -a`, disk-pressure GC or a host rebuild" stays
# survivable, and restore_image() recovers in three steps — local, then the
# immutable per-sha tag in Harbor, then `:rollback` in Harbor. Host-local
# images are a cache, not the source of truth. Harbor retention is a separate
# concern with its own rules; see scripts/registry-prune.py.
#
# The retention windows below are belt-and-braces on top of that: IMAGE_MAX_AGE
# keeps the last couple of days of images resident so the common rollback does
# not even need a pull, and `docker image prune` only ever removes images no
# container references, so the running stack is never a candidate.
#
#   scripts/host-gc.sh            prune, print reclaimed space
#   scripts/host-gc.sh --dry-run  report what is resident now, change nothing
#
# Exits 0 even when a prune fails: a GC problem must never fail an otherwise
# healthy deploy. Failures are logged loudly for the next heartbeat to notice.
set -uo pipefail

# Unused images older than this are removed. Must stay comfortably longer than
# the gap between consecutive deploys so the previous release — the one
# `:rollback` points at — is normally still resident locally.
IMAGE_MAX_AGE="${IMAGE_MAX_AGE:-48h}"
# Build cache records unused for longer than this are removed. Keeping a couple
# of days means the usual incremental build still hits warm layers.
CACHE_MAX_AGE="${CACHE_MAX_AGE:-48h}"
# Warn when the root filesystem is still this full after a prune — the signal
# that retention is too generous or something else is eating the disk.
WARN_PCT="${WARN_PCT:-80}"

log(){ echo "[host-gc] $*"; }

disk_pct(){ df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}'; }
disk_line(){ df -h / | awk 'NR==2 {print $3" used, "$4" free ("$5" of "$2")"}'; }

log "before: $(disk_line)"

if [ "${1:-}" = "--dry-run" ]; then
  log "dry-run — nothing will be removed"
  docker system df || true
  exit 0
fi

rc=0

# Build cache first: it is pure derived data, so it is both the safest thing to
# drop and usually the largest single win.
if ! docker builder prune -f --filter "until=$CACHE_MAX_AGE" 2>&1 | sed 's/^/[host-gc] /'; then
  log "WARN: builder prune failed"
  rc=1
fi

# Then unused images. -a also considers tagged-but-unreferenced images, which
# is the whole point: the accumulation is per-sha release tags that no
# container uses any more. The until= filter is what keeps the recent ones.
if ! docker image prune -af --filter "until=$IMAGE_MAX_AGE" 2>&1 | sed 's/^/[host-gc] /'; then
  log "WARN: image prune failed"
  rc=1
fi

log "after:  $(disk_line)"

pct="$(disk_pct)"
if [ -n "$pct" ] && [ "$pct" -ge "$WARN_PCT" ] 2>/dev/null; then
  log "WARN: root filesystem still at ${pct}% after GC (threshold ${WARN_PCT}%)."
  log "WARN: retention may be too generous, or non-Docker data is growing."
  docker system df || true
fi

[ "$rc" = 0 ] || log "completed with warnings (exit forced to 0 so the deploy is not failed)"
exit 0
