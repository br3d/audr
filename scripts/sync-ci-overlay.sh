#!/usr/bin/env bash
# Sync ci/gitea-overlay/workflows/ -> the deploy host's mirror overlay.
#
# MAINTENANCE ONLY — this is not how a workflow change ships. Since AUD-443 the
# mirror reads the workflow content out of the main commit it is mirroring, so
# ci/gitea-overlay/workflows/ IS what runs: commit, push to main, done.
#
# The host directory ~/.config/audr-mirror/overlay/.gitea/workflows/ now only
# supplies files the commit does not carry. This script still reconciles the two,
# which is worth doing to prune a stale host copy so that fallback cannot
# surprise anyone. See ci/gitea-overlay/README.md.
#
#   scripts/sync-ci-overlay.sh           diff, then push on confirmation
#   scripts/sync-ci-overlay.sh --check   diff only; exit 1 on drift (CI/drift guard)
#   scripts/sync-ci-overlay.sh --yes     push without the prompt
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/lib/deploy-env.sh
. "$REPO_DIR/scripts/lib/deploy-env.sh"
audr_load_deploy_env "$REPO_DIR"

SRC="$REPO_DIR/ci/gitea-overlay/workflows"
HOST="${AUDR_DEPLOY_HOST:-}"
audr_require AUDR_DEPLOY_HOST "SSH destination of the deploy host that runs the mirror, e.g. deploy@audr.example.internal."
KEY="${AUDR_SSH_KEY:-$REPO_DIR/id_ed25519}"
REMOTE_DIR=".config/audr-mirror/overlay/.gitea/workflows"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$HOST")

mode=apply
case "${1:-}" in
  --check) mode=check ;;
  --yes)   mode=force ;;
  "")      ;;
  *) echo "usage: $0 [--check|--yes]" >&2; exit 2 ;;
esac

[ -d "$SRC" ] || { echo "missing $SRC" >&2; exit 2; }

# Fetch rather than `cat` over ssh: command substitution strips trailing
# newlines, which shows up as phantom drift on files that lack one.
fetched="$(mktemp -d)"
trap 'rm -rf "$fetched"' EXIT
scp -q -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  "$HOST:$REMOTE_DIR/*.yaml" "$fetched/" 2>/dev/null || true

drift=0
for path in "$SRC"/*.yaml; do
  f="$(basename "$path")"
  if [ ! -f "$fetched/$f" ]; then
    echo "== $f: absent on host"
    drift=1
  elif ! diff -u --label "host/$f" --label "repo/$f" "$fetched/$f" "$path"; then
    drift=1
  fi
done

if [ "$drift" -eq 0 ]; then
  echo "==> overlay in sync"
  exit 0
fi

if [ "$mode" = check ]; then
  echo "==> DRIFT: host overlay differs from ci/gitea-overlay/workflows" >&2
  exit 1
fi

if [ "$mode" = apply ]; then
  read -r -p "Push the repo copies to $HOST:$REMOTE_DIR? [y/N] " reply
  [ "$reply" = y ] || [ "$reply" = Y ] || { echo "aborted"; exit 1; }
fi

# Timestamped backup first: a bad workflow file breaks every deploy.
stamp="$("${SSH[@]}" 'date -u +%Y%m%dT%H%M%SZ')"
"${SSH[@]}" "mkdir -p $REMOTE_DIR ~/.config/audr-mirror/archive/overlay-$stamp && cp -a $REMOTE_DIR/. ~/.config/audr-mirror/archive/overlay-$stamp/ 2>/dev/null || true"
echo "==> host copies backed up to ~/.config/audr-mirror/archive/overlay-$stamp"

scp -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  "$SRC"/*.yaml "$HOST:$REMOTE_DIR/"
echo "==> pushed. The next audr-mirror.timer tick (every 15m) rebuilds Gitea main"
echo "    only if GitHub main also moved — force a rebuild with:"
echo "    ssh $HOST 'rm ~/.config/audr-mirror/last-main-sha && ~/bin/audr-github-mirror.sh'"
