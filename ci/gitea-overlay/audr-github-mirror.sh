#!/usr/bin/env bash
# GitHub br3d/audr -> Gitea dfbot/audr one-way sync.
# - Pulls all refs from GitHub over SSH (read-only deploy key).
# - Pushes all branches (except main) + tags to Gitea verbatim.
# - For main: Gitea main = GitHub main + .gitea/workflows overlay (CI config that
#   lives only on the Gitea side). Rebuilt when GitHub main changes, so CI
#   fires once per real upstream change and history stays clean.
#
# THE BOOKMARK MEANS DEPLOYED, NOT MIRRORED (AUD-706)
# ---------------------------------------------------
# This script used to write `last-main-sha` the instant it pushed, before any CI
# result existed, and then skip every later tick that saw the same GitHub main.
# A deploy is a *consequence* of that push, not a precondition of the
# bookkeeping, so a commit whose tests went red — `build-deploy` correctly
# `skipped` by AUD-687's gate — was recorded as shipped and never retried. The
# stand kept serving its previous image, perfectly healthy and not `main`, until
# somebody merged something else. `/health/ready` cannot see that, because the
# old image answers it just fine.
#
# AUD-701 is the worked example: run #470 lost its test containers to a reboot,
# the gate skipped the deploy, and the stand sat on an image built from a
# *scratch branch* by a dispatch run. It took a human noticing.
#
# So the mirror now tracks two facts, not one:
#
#   last-main-sha        GitHub main this script last mirrored  (its own
#                        idempotency marker; written at push time, unchanged
#                        semantics, so nothing downstream has to be re-taught)
#   mirrored-gitea-sha   the Gitea main commit it pushed for that GitHub sha
#   mirrored-at          when it pushed that commit, so a pipeline that is
#                        merely still running is not mistaken for a failure
#   deployed-gitea-sha   the Gitea main commit the deploy workflow last
#                        *successfully* deployed — written by deploy.yaml on its
#                        success path, on this host, as this user
#
# `deployed == mirrored` is the only thing that means "main is live". Comparing
# Gitea shas rather than GitHub ones is deliberate: it costs the deploy job
# nothing (GITHUB_SHA already *is* that value, no parent-walking, no commit
# message parsing) and it makes a tag or dispatch deploy register as what it is.
# A dispatch build of a scratch branch writes a sha that matches no mirrored
# main, so it cannot be mistaken for having shipped main — which is exactly the
# confusion that let AUD-701 run unnoticed.
#
# When they disagree the commit is mirrored-but-undeployed, and this script
# retries the deploy by rebuilding Gitea main (a fresh overlay commit, so CI
# fires again). Retries are capped — a genuinely red commit would otherwise
# rebuild every 15 minutes forever — and the cap being reached raises exactly
# one board alert under a single --key, the way AUD-580 gave disk a voice.
set -euo pipefail

CFG="${AUDR_MIRROR_CFG:-$HOME/.config/audr-mirror}"
MIRROR_DIR="${AUDR_MIRROR_DIR:-$HOME/audr-mirror.git}"
# The three overrides above and below exist only so the shipped script is the
# thing under test: backend/tests/unit/test_mirror_deploy_state.py points them
# at scratch repositories on disk and drives the real sync, retry and alert
# paths. The defaults are the production values, so the host needs no
# environment at all.
GH_URL="${AUDR_MIRROR_GH_URL:-git@github.com:br3d/audr.git}"
OVERLAY="$CFG/overlay"
STATE="$CFG/last-main-sha"
MIRRORED="$CFG/mirrored-gitea-sha"
MIRRORED_AT="$CFG/mirrored-at"
DEPLOYED="$CFG/deployed-gitea-sha"
UNDEPLOYED="$CFG/undeployed-state"
ALERT_KEY=main-undeployed

# How long a freshly mirrored commit is allowed to be undeployed before that
# counts as a failure rather than as a pipeline still running.
#
# This is load-bearing, not a safety margin. The timer ticks every 15 minutes
# and the pipeline (three test jobs, then build + deploy, all serialized by one
# `concurrency` group) has measured 3 to 29 minutes on this runner — so without
# a grace period a tick lands mid-deploy, sees mirrored != deployed, and
# "retries" a commit that was busy succeeding. Worse than wasteful: the retry
# re-mirrors, Gitea main moves, and the in-flight run then records a sha that no
# longer matches, so the next tick alerts about a deploy that worked.
#
# 60 minutes is comfortably past the worst observed run plus queueing. The cost
# of erring high is that a genuinely failed deploy waits an hour for its retry,
# which is cheap: nothing is broken, main is simply not live yet, and the
# alternative — crying wolf on every slow deploy — is how an alert channel gets
# muted. That is the failure mode AUD-444 exists to avoid.
#
# The obvious alternative, asking Gitea for the run's conclusion, is rejected
# for the same reason deploy.yaml's header rejects it for the gate: it needs a
# credential this script has no business using for reads, and it trades a clock
# for an API dependency on the box the mirror exists to feed.
GRACE="${AUDR_MIRROR_DEPLOY_GRACE:-3600}"

# One retry by default. The first rebuild covers the large majority of the
# cases this exists for — an infrastructure-flavoured failure (a killed test
# container, a runner that lost its host mid-run, a transient registry error)
# that succeeds on a second run against the identical tree. A second retry buys
# very little and costs a full suite plus a deploy lock each time, so beyond the
# cap the answer is a human, not another build.
MAX_RETRIES="${AUDR_MIRROR_DEPLOY_RETRIES:-1}"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

read_state() { cat "$1" 2>/dev/null || true; }

# notify.sh is not in this script's directory — the mirror runs out of ~/bin
# against a bare repo, with no checkout to call into. The watchdog's installed
# tree is the host's copy of the alerting scripts and already carries the
# board credential path this needs, so reuse it rather than installing a second
# copy that could drift. Absent (or unconfigured) is not an error: notify.sh's
# own contract is that an alerting helper must never be the thing that breaks,
# and that applies doubly to a caller whose real job is mirroring.
NOTIFY="${AUDR_NOTIFY_SH:-$HOME/.local/share/audr-watchdog/scripts/notify.sh}"
notify() {
  if [ ! -f "$NOTIFY" ]; then
    log "notify.sh not found at $NOTIFY; alert not sent: $*"
    return 0
  fi
  AUDR_REMOTE_DIR="${AUDR_REMOTE_DIR:-$HOME/audr}" \
    bash "$NOTIFY" --key "$ALERT_KEY" "$@" || true
}

# ---------------------------------------------------------------------------
# Decision
#
# Split out from the work so it can be read, and tested, without a network: the
# whole bug this addresses was a one-line ordering mistake inside a script that
# only ran against the live host. backend/tests/unit/test_mirror_deploy_state.py
# drives this through `--decide` over a scratch $AUDR_MIRROR_CFG.
#
#   rebuild    GitHub main moved; mirror it (the ordinary path)
#   deployed   mirrored commit is live; nothing to do
#   in-flight  mirrored recently and not deployed yet; the pipeline is
#              presumed still running (see $GRACE)
#   retry      mirrored commit never deployed; rebuild to re-fire CI
#   alert      retries exhausted; raise the board alert
#   waiting    retries exhausted and already alerted; stay quiet
#   bootstrap  no mirrored-gitea-sha on record (pre-AUD-706 state file)
# ---------------------------------------------------------------------------
decide() {
  local gh_main="$1" last mirrored mirrored_at deployed key tries alerted now
  # Overridable so the grace-period branches are testable without sleeping.
  now="${AUDR_MIRROR_NOW:-$(date -u +%s)}"

  last="$(read_state "$STATE")"
  [ -n "$last" ] || last=none
  if [ "$gh_main" != "$last" ]; then
    echo rebuild
    return 0
  fi

  mirrored="$(read_state "$MIRRORED")"
  if [ -z "$mirrored" ]; then
    # Upgrading onto a host whose state predates this mechanism. The Gitea sha
    # for the already-mirrored commit was never recorded and cannot be
    # recovered from here, so there is nothing to compare: do not retry (a
    # needless rebuild of a commit that may well be live) and do not alert (a
    # false alarm). The next real main change records it and the mechanism
    # engages from then on — self-healing within one merge.
    echo bootstrap
    return 0
  fi

  deployed="$(read_state "$DEPLOYED")"
  if [ "$deployed" = "$mirrored" ]; then
    echo deployed
    return 0
  fi

  # Undeployed, but possibly only because the pipeline is still running. See
  # $GRACE above: ticks are more frequent than deploys are long, so this branch
  # is the common case for a perfectly healthy deploy, not an edge case.
  mirrored_at="$(read_state "$MIRRORED_AT")"
  if [ -z "$mirrored_at" ]; then
    # A sha with no timestamp beside it. The two are written together, so this
    # is only a partially-upgraded host — and the safe reading is the same one
    # as a missing sha: the deploy state is unknown. Guessing "long ago" would
    # retry a commit that may be mid-deploy, which is the precise thing $GRACE
    # exists to prevent. Sync mode stamps it (see below) so this self-heals on
    # the first tick instead of waiting for the next merge.
    echo bootstrap
    return 0
  fi
  if [ "$((now - mirrored_at))" -lt "$GRACE" ]; then
    echo in-flight
    return 0
  fi

  # `|| true` because read reports EOF on an empty or absent state file, which
  # is a perfectly ordinary first-run condition and not a failure.
  read -r key tries alerted <<<"$(read_state "$UNDEPLOYED")" || true
  if [ "${key:-}" != "$gh_main" ]; then tries=0; alerted=0; fi
  tries="${tries:-0}"; alerted="${alerted:-0}"

  if [ "$tries" -lt "$MAX_RETRIES" ]; then
    echo retry
  elif [ "$alerted" != 1 ]; then
    echo alert
  else
    echo waiting
  fi
}

# ---------------------------------------------------------------------------
# Gitea main = GitHub main + .gitea/workflows overlay
# ---------------------------------------------------------------------------
rebuild_main() {
  local gh_main="$1" attempt="${2:-0}" gitea_url covered blob name rel tree new f msg
  gitea_url="$(cat "$CFG/gitea-push-url")"

  export GIT_INDEX_FILE; GIT_INDEX_FILE="$(mktemp)"
  git read-tree "$gh_main"

  # Workflow content comes from the commit being mirrored
  # (ci/gitea-overlay/workflows/) whenever that path exists there, so the repo
  # is the single source of truth for what actually executes. It used to come
  # only from $OVERLAY on this host, which meant a commit could change the
  # versioned workflow while the stale host copy kept running — AUD-443: a
  # pre-split deploy.yaml copied compose.yaml without its compose.deploy.yaml
  # overlay, silently downgraded the stand to the public image and took it down
  # for two hours. Reading from the commit removes that failure mode rather than
  # relying on someone remembering scripts/sync-ci-overlay.sh.
  covered=""
  if git rev-parse --quiet --verify "$gh_main:ci/gitea-overlay/workflows" >/dev/null; then
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      blob="$(git rev-parse "$gh_main:ci/gitea-overlay/workflows/$name")"
      git update-index --add --cacheinfo "100644,$blob,.gitea/workflows/$name"
      covered="$covered .gitea/workflows/$name"
    done < <(git ls-tree --name-only "$gh_main:ci/gitea-overlay/workflows")
    log "overlay from commit: ${covered# }"
  else
    log "commit has no ci/gitea-overlay/workflows; falling back to $OVERLAY entirely"
  fi

  # $OVERLAY still contributes anything the commit does not carry, so a
  # host-only file (or an older commit predating the versioned copies) keeps
  # working. Files the commit does provide are NOT overridden from the host.
  while IFS= read -r -d '' f; do
    rel="${f#"$OVERLAY"/}"
    case " $covered " in *" $rel "*) continue ;; esac
    blob="$(git hash-object -w "$f")"
    git update-index --add --cacheinfo "100644,$blob,$rel"
    log "overlay from host: $rel"
  done < <(find "$OVERLAY" -type f -print0)
  tree="$(git write-tree)"
  rm -f "$GIT_INDEX_FILE"; unset GIT_INDEX_FILE

  # The mirrored GitHub commit is the parent, and the message names it, so the
  # GitHub sha behind any Gitea main commit stays recoverable by hand even
  # though nothing in the retry logic has to parse either any more.
  msg="ci(gitea): sync GitHub main ($gh_main) + .gitea/workflows overlay [AUD-294]"

  # A retry MUST produce a different commit, and the attempt number in the
  # message is what guarantees it. `git commit-tree` is a pure function of its
  # inputs, and on a retry the tree, the parent, the author and the message are
  # all identical to the previous attempt's — so the only thing left to vary the
  # sha is the committer timestamp. Leaning on the clock would make this work
  # only because ticks are 15 minutes apart: with the same second, the sha is
  # identical, `git push` reports everything-up-to-date, Gitea fires no run, and
  # the retry does nothing at all while logging that it retried. Caught by
  # test_undeployed_main_is_rebuilt_once_then_alerted_once, which runs the ticks
  # back to back for exactly this reason. It also makes the history say out loud
  # why the same commit was mirrored twice.
  if [ "$attempt" -ge 1 ]; then
    msg="$msg

Deploy retry $attempt/$MAX_RETRIES: the previous mirror of this commit never
deployed (build-deploy skipped or failed), so main was not live. Re-mirrored to
re-run the pipeline against the identical tree. See AUD-706."
  fi

  new="$(GIT_AUTHOR_NAME=infraLead GIT_AUTHOR_EMAIL=info@redgear.me \
         GIT_COMMITTER_NAME=infraLead GIT_COMMITTER_EMAIL=info@redgear.me \
         git commit-tree "$tree" -p "$gh_main" -m "$msg")"
  git push "$gitea_url" "+$new:refs/heads/main"

  # Both files, and in this order. `mirrored-gitea-sha` is what the undeployed
  # check reads; writing `last-main-sha` first would re-open a one-tick window
  # where main looks mirrored with no Gitea sha on record, which is the
  # bootstrap state and would silently skip a tick's worth of checking.
  echo "$new" > "$MIRRORED"
  # The grace period is measured from here, and a retry restarts it — each
  # attempt gets a full pipeline's worth of time to succeed.
  date -u +%s > "$MIRRORED_AT"
  echo "$gh_main" > "$STATE"
  log "Gitea main -> $new"
}

mode=sync
case "${1:-}" in
  --decide) mode=decide ;;
  "") ;;
  *) echo "usage: $0 [--decide]" >&2; exit 2 ;;
esac

mkdir -p "$CFG"

if [ "$mode" = decide ]; then
  # No fetch, no push, no state writes. $AUDR_MIRROR_GH_MAIN stands in for the
  # fetched ref so the matrix can be driven without a repository at all.
  decide "${AUDR_MIRROR_GH_MAIN:-$(git --git-dir="$MIRROR_DIR" rev-parse refs/heads/main)}"
  exit 0
fi

[ -d "$MIRROR_DIR" ] || git clone --bare "$GH_URL" "$MIRROR_DIR"
cd "$MIRROR_DIR"

export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/github_mirror -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
git fetch --prune "$GH_URL" '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'

# Push every branch except main, plus all tags, verbatim.
#
# dependabot/* is skipped. Dependabot's branches live and die on the GitHub
# side — it deletes them once its pull request is merged or closed — but this
# mirror never propagates a deletion (it pushes `+ref:ref` and nothing else), so
# every one it replayed would sit in Gitea forever. They also carry no
# .gitea/workflows, so nothing on the Gitea side has any use for them.
refspecs=()
while read -r ref; do
  [ "$ref" = "refs/heads/main" ] && continue
  case "$ref" in refs/heads/dependabot/*) continue ;; esac
  refspecs+=("+$ref:$ref")
done < <(git for-each-ref --format='%(refname)' refs/heads/)
# `+refs/tags/*` last, and the array guarded: with no branches besides main and
# no tags yet, `git push <url> '+refs/tags/*:refs/tags/*'` fails the whole run
# ("No refs in common and none specified") before main is ever considered, and
# on bash < 4.4 the empty `${refspecs[@]}` is itself an unbound-variable error
# under `set -u`. Neither has fired on this host — the repo has tags — but both
# are a fresh mirror's first run, and a mirror that dies before the main branch
# is the failure this script's whole header is about.
if [ "${#refspecs[@]}" -gt 0 ] || [ -n "$(git for-each-ref --format='%(refname)' refs/tags/)" ]; then
  git push "$(cat "$CFG/gitea-push-url")" ${refspecs[@]+"${refspecs[@]}"} '+refs/tags/*:refs/tags/*'
else
  log "no branches besides main and no tags to mirror verbatim"
fi

GH_MAIN="$(git rev-parse refs/heads/main)"
LAST="$(read_state "$STATE")"; [ -n "$LAST" ] || LAST=none

# Self-heal a half-written state pair before deciding on it. `mirrored-at` was
# added after `mirrored-gitea-sha`, so a host upgraded between the two versions
# has the sha and no stamp. Starting its grace period now is the conservative
# choice — it grants a full pipeline's worth of time to a deploy that may well
# be in flight right now — and it means the check engages on this tick rather
# than sitting in `bootstrap` until the next merge.
if [ -n "$(read_state "$MIRRORED")" ] && [ -z "$(read_state "$MIRRORED_AT")" ]; then
  date -u +%s > "$MIRRORED_AT"
  log "mirrored-gitea-sha had no mirrored-at beside it; starting the grace period from now"
fi

action="$(decide "$GH_MAIN")"

case "$action" in
  rebuild)
    log "GitHub main changed ($LAST -> $GH_MAIN); rebuilding Gitea main + CI overlay"
    rebuild_main "$GH_MAIN"
    # A new commit is its own retry budget. Resetting here rather than on the
    # retry paths keeps the counter keyed to the commit it describes.
    echo "$GH_MAIN 0 0" > "$UNDEPLOYED"
    ;;

  deployed)
    log "GitHub main unchanged ($GH_MAIN) and deployed as $(read_state "$MIRRORED"); overlay preserved"
    # Resolve on the tick that observes a live main, not on the tick that
    # mirrors a new commit — mirroring is the thing that was never evidence of
    # a deploy. Worst-case resolution lag is one timer period. --resolve
    # against a key with nothing open is a no-op that exits 0, so this is
    # silent after the overwhelming majority of ticks.
    if [ -s "$UNDEPLOYED" ] && [ "$(cut -d' ' -f3 "$UNDEPLOYED")" = 1 ]; then
      log "clearing the undeployed-main alert"
      notify --resolve -- "$(printf '%s\n' \
        "✅ audr main is deployed again" \
        "" \
        "GitHub main  : $GH_MAIN" \
        "Gitea main   : $(read_state "$MIRRORED")" \
        "" \
        "A later deploy of this commit succeeded. Closing the undeployed-main alert.")"
    fi
    echo "$GH_MAIN 0 0" > "$UNDEPLOYED"
    ;;

  retry)
    read -r _ tries _ <<<"$(read_state "$UNDEPLOYED")" || true; tries="${tries:-0}"
    log "GitHub main $GH_MAIN was mirrored as $(read_state "$MIRRORED") but never deployed (deployed-gitea-sha=$(read_state "$DEPLOYED")); retry $((tries + 1))/$MAX_RETRIES"
    # Write the counter BEFORE the rebuild. If the push or the mkdir fails
    # half-way, the attempt still counts — the alternative is a crash loop that
    # retries forever and never reaches the alert that would tell anyone.
    echo "$GH_MAIN $((tries + 1)) 0" > "$UNDEPLOYED"
    rebuild_main "$GH_MAIN" "$((tries + 1))"
    ;;

  alert)
    log "GitHub main $GH_MAIN is mirrored but undeployed after $MAX_RETRIES retr(y|ies); alerting"
    notify -- "$(printf '%s\n' \
      "🚨 audr main is mirrored but NOT deployed" \
      "" \
      "GitHub main      : $GH_MAIN" \
      "Gitea main       : $(read_state "$MIRRORED")" \
      "last deployed    : $(read_state "$DEPLOYED") (a different commit)" \
      "retries spent    : $MAX_RETRIES" \
      "" \
      "The deploy for this commit did not succeed — most likely a red test job," \
      "which leaves build-deploy 'skipped' by design (AUD-687). The stand is" \
      "still serving its previous image and is HEALTHY; it is just not main." \
      "/health/ready cannot tell you this, which is why this alert exists." \
      "" \
      "What to do:" \
      "  1. Read the failing jobs for Gitea main on the runner host." \
      "  2. Fix forward on main. The next commit deploys normally and this" \
      "     alert closes itself on the following mirror tick." \
      "  3. For an emergency, the host-side manual deploy in" \
      "     docs/deploy-runbook.md bypasses CI as a deliberate, logged act." \
      "" \
      "The mirror will NOT rebuild this commit again — the retry budget is spent," \
      "so nothing is looping and no further alert will be raised for it.")"
    echo "$GH_MAIN $MAX_RETRIES 1" > "$UNDEPLOYED"
    ;;

  in-flight)
    log "GitHub main $GH_MAIN mirrored as $(read_state "$MIRRORED") $(( ($(date -u +%s) - $(read_state "$MIRRORED_AT")) / 60 ))m ago and not deployed yet; inside the ${GRACE}s grace period — the pipeline is presumed still running"
    ;;

  waiting)
    log "GitHub main $GH_MAIN still undeployed; retries spent and already alerted — no action"
    ;;

  bootstrap)
    log "GitHub main unchanged ($GH_MAIN); no mirrored-gitea-sha on record (pre-AUD-706 state) — deploy state unknown, overlay preserved"
    ;;
esac

log "sync OK"
