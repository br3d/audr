#!/usr/bin/env bash
# Bound the deploy host's Docker disk usage.
#
# Why this exists (AUD-395): every push to main runs deploy.yaml, which builds
# and tags a new `audr-backend:<sha>` image on the host runner. Nothing ever
# removed them. By 2026-10-03 the deploy host had accumulated 299
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
# Why age alone is not enough (AUD-425). A pure age window is rate-blind: it
# retains "two days of deploys", which is a handful of images on a quiet day
# and well over a dozen (~10GB) on a busy one. On 2026-10-04 the host was back
# at 80% — the warn threshold — with ~20 release images all younger than the
# 48h window, so GC ran, found nothing eligible, and logged a warning instead
# of acting on it. Two mechanisms fix that without tightening the default:
#
#   * IMAGE_KEEP_COUNT caps how many unused release images stay resident
#     regardless of age, so the floor is a count, not a date.
#   * If the filesystem is still at or above WARN_PCT after the normal pass,
#     the script escalates on its own to the much shorter ESCALATE_* windows
#     and says so in the log. Disk pressure no longer waits for a human.
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
# At most this many *unused* `audr-*` release images stay resident, newest
# first, however young they are. The age window above is the relaxed default;
# this is the rate-blind backstop for a day with a dozen merges. Five is
# comfortably more than the rollback path needs (it only wants the previous
# release, and can pull from the registry even without that).
IMAGE_KEEP_COUNT="${IMAGE_KEEP_COUNT:-5}"
# Warn when the root filesystem is still this full after a prune — the signal
# that retention is too generous or something else is eating the disk. At or
# above this mark the script also escalates to the ESCALATE_* windows below
# instead of only warning.
WARN_PCT="${WARN_PCT:-80}"
# Retention used for the second, escalated pass. Deliberately aggressive: by
# the time it runs, the normal windows have already been shown to be too
# generous for the current merge rate, and a host that runs out of disk takes
# the live database volume with it.
ESCALATE_IMAGE_MAX_AGE="${ESCALATE_IMAGE_MAX_AGE:-6h}"
ESCALATE_CACHE_MAX_AGE="${ESCALATE_CACHE_MAX_AGE:-6h}"
ESCALATE_IMAGE_KEEP_COUNT="${ESCALATE_IMAGE_KEEP_COUNT:-2}"
# ci.yaml names its compose project `audr-test-ci-<run>-<attempt>`, so the test
# images it builds are unique per run and can never be a cache hit for a later
# one. ci.yaml deletes its own at teardown; this window only has to cover the
# case where the runner was killed before that step ran, so it is short.
TEST_IMAGE_MAX_AGE_HOURS="${TEST_IMAGE_MAX_AGE_HOURS:-6}"

log(){ echo "[host-gc] $*"; }

disk_pct(){ df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}'; }
disk_line(){ df -h / | awk 'NR==2 {print $3" used, "$4" free ("$5" of "$2")"}'; }

# Full image IDs that the count cap must never consider, whatever their age:
# anything a container (running or stopped) is built from, plus whatever the
# `:latest` and `:rollback` tags currently point at. The first is what keeps
# the live stack safe, the second is what keeps the no-pull rollback fast.
protected_image_ids(){
  docker ps -aq 2>/dev/null | xargs -r docker inspect --format '{{.Image}}' 2>/dev/null
  docker images --no-trunc --format '{{.ID}} {{.Tag}}' 2>/dev/null \
    | awk '$2 == "latest" || $2 == "rollback" {print $1}'
}

# Remove unused `audr-*` release images beyond the newest $1, oldest first.
#
# Deliberately `docker image rm` without `-f`: docker refuses to remove an
# image a container references, so even if the protected set were somehow
# incomplete, the running stack is still safe by construction. Tags are
# removed by reference rather than by ID so that an image carrying a second,
# protected tag only loses its per-sha alias.
cap_release_images(){
  local keep="$1" protected candidates victim removed=0
  protected="$(protected_image_ids | sed 's/^/ /;s/$/ /' | sort -u)"

  # `docker images` lists newest first; dropping the head of that list leaves
  # the oldest, which are the ones to reap.
  candidates="$(
    docker images --no-trunc --format '{{.ID}}|{{.Repository}}:{{.Tag}}|{{.Repository}}' 2>/dev/null \
      | awk -F'|' '{ n = split($3, p, "/"); base = p[n] }
                   base ~ /^audr-/ && $2 !~ /:(latest|rollback|<none>)$/ {print $1"|"$2}'
  )"
  [ -n "$candidates" ] || return 0

  while IFS='|' read -r id ref; do
    [ -n "$ref" ] || continue
    case "$protected" in *" $id "*) continue ;; esac
    if [ "$keep" -gt 0 ]; then
      keep=$((keep - 1))
      continue
    fi
    victim="$ref"
    if docker image rm "$victim" >/dev/null 2>&1; then
      removed=$((removed + 1))
    else
      log "kept $victim (still referenced)"
    fi
  done <<EOF
$candidates
EOF

  [ "$removed" = 0 ] || log "count cap removed $removed unused release image(s) beyond the newest $1"
}

# One full prune pass. Age windows and the count cap are parameters so the
# escalated second pass can reuse the whole thing with tighter numbers.
prune_pass(){
  local cache_age="$1" image_age="$2" keep="$3"

  # Build cache first: it is pure derived data, so it is both the safest thing
  # to drop and usually the largest single win.
  if ! docker builder prune -f --filter "until=$cache_age" 2>&1 | sed 's/^/[host-gc] /'; then
    log "WARN: builder prune failed"
    rc=1
  fi

  # Then unused images. -a also considers tagged-but-unreferenced images, which
  # is the whole point: the accumulation is per-sha release tags that no
  # container uses any more. The until= filter is what keeps the recent ones.
  if ! docker image prune -af --filter "until=$image_age" 2>&1 | sed 's/^/[host-gc] /'; then
    log "WARN: image prune failed"
    rc=1
  fi

  # Finally the rate-blind backstop for everything inside the age window.
  cap_release_images "$keep"
}

log "before: $(disk_line)"

if [ "${1:-}" = "--dry-run" ]; then
  log "dry-run — nothing will be removed"
  docker system df || true
  exit 0
fi

rc=0

prune_pass "$CACHE_MAX_AGE" "$IMAGE_MAX_AGE" "$IMAGE_KEEP_COUNT"

# Orphaned per-run CI test images. These are worthless the moment their run
# ends (see TEST_IMAGE_MAX_AGE_HOURS), so they do not deserve the 48h window
# the release images get. `docker image rm` without -f refuses an image a
# container still references, which is what keeps a live concurrent CI job on
# the other runner slot safe; the age window is the second guard.
cutoff=$(( $(date +%s) - TEST_IMAGE_MAX_AGE_HOURS * 3600 ))
reaped=0
for id in $(docker images --filter 'reference=audr-test-ci-*' -q 2>/dev/null | sort -u); do
  created="$(docker image inspect --format '{{.Created}}' "$id" 2>/dev/null)" || continue
  [ -n "$created" ] || continue
  created_ts="$(date -d "$created" +%s 2>/dev/null)" || continue
  if [ -z "$created_ts" ] || [ "$created_ts" -ge "$cutoff" ]; then continue; fi
  if docker image rm "$id" >/dev/null 2>&1; then
    reaped=$((reaped + 1))
  fi
done
[ "$reaped" = 0 ] || log "reaped $reaped orphaned CI test image(s) older than ${TEST_IMAGE_MAX_AGE_HOURS}h"

log "after:  $(disk_line)"

# Still under pressure? The normal windows have just been demonstrated to be
# too generous for the current merge rate, so tighten them here rather than
# waiting for someone to read the warning. Safety is unchanged — the escalated
# pass uses the same prune primitives and the same protected set.
pct="$(disk_pct)"
if [ -n "$pct" ] && [ "$pct" -ge "$WARN_PCT" ] 2>/dev/null; then
  log "WARN: root filesystem still at ${pct}% after GC (threshold ${WARN_PCT}%)."
  log "escalating: cache<${ESCALATE_CACHE_MAX_AGE}, images<${ESCALATE_IMAGE_MAX_AGE}, keep newest ${ESCALATE_IMAGE_KEEP_COUNT}"
  prune_pass "$ESCALATE_CACHE_MAX_AGE" "$ESCALATE_IMAGE_MAX_AGE" "$ESCALATE_IMAGE_KEEP_COUNT"
  log "after escalation: $(disk_line)"

  pct="$(disk_pct)"
  if [ -n "$pct" ] && [ "$pct" -ge "$WARN_PCT" ] 2>/dev/null; then
    log "WARN: root filesystem STILL at ${pct}% after escalated GC."
    log "WARN: Docker is no longer the thing filling this disk — investigate non-Docker growth."
    docker system df || true
  fi
fi

[ "$rc" = 0 ] || log "completed with warnings (exit forced to 0 so the deploy is not failed)"
exit 0
