#!/usr/bin/env bash
# GitHub br3d/audr -> Gitea dfbot/audr one-way sync (AUD-294).
# - Pulls all refs from GitHub over SSH (read-only deploy key).
# - Pushes all branches (except main) + tags to Gitea verbatim.
# - For main: Gitea main = GitHub main + .gitea/workflows overlay (CI config that
#   lives only on the Gitea side). Rebuilt ONLY when GitHub main changes, so CI
#   fires once per real upstream change and history stays clean.
set -euo pipefail

CFG="$HOME/.config/audr-mirror"
MIRROR_DIR="$HOME/audr-mirror.git"
GH_URL="git@github.com:br3d/audr.git"
GITEA_URL="$(cat "$CFG/gitea-push-url")"
OVERLAY="$CFG/overlay"
STATE="$CFG/last-main-sha"
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/github_mirror -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

log() { echo "[$(date -u +%FT%TZ)] $*"; }

[ -d "$MIRROR_DIR" ] || git clone --bare "$GH_URL" "$MIRROR_DIR"
cd "$MIRROR_DIR"

git fetch --prune "$GH_URL" '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'

# Push every branch except main, plus all tags, verbatim.
refspecs=()
while read -r ref; do
  [ "$ref" = "refs/heads/main" ] && continue
  refspecs+=("+$ref:$ref")
done < <(git for-each-ref --format='%(refname)' refs/heads/)
git push "$GITEA_URL" "${refspecs[@]}" '+refs/tags/*:refs/tags/*'

# main: rebuild overlay commit only when GitHub main moved.
GH_MAIN="$(git rev-parse refs/heads/main)"
LAST="$(cat "$STATE" 2>/dev/null || echo none)"
if [ "$GH_MAIN" != "$LAST" ]; then
  log "GitHub main changed ($LAST -> $GH_MAIN); rebuilding Gitea main + CI overlay"
  export GIT_INDEX_FILE; GIT_INDEX_FILE="$(mktemp)"
  git read-tree "$GH_MAIN"
  while IFS= read -r -d '' f; do
    rel="${f#"$OVERLAY"/}"
    blob="$(git hash-object -w "$f")"
    git update-index --add --cacheinfo "100644,$blob,$rel"
  done < <(find "$OVERLAY" -type f -print0)
  tree="$(git write-tree)"
  rm -f "$GIT_INDEX_FILE"; unset GIT_INDEX_FILE
  new="$(GIT_AUTHOR_NAME=infraLead GIT_AUTHOR_EMAIL=info@redgear.me \
         GIT_COMMITTER_NAME=infraLead GIT_COMMITTER_EMAIL=info@redgear.me \
         git commit-tree "$tree" -p "$GH_MAIN" \
         -m "ci(gitea): sync GitHub main ($GH_MAIN) + .gitea/workflows overlay [AUD-294]")"
  git push "$GITEA_URL" "+$new:refs/heads/main"
  echo "$GH_MAIN" > "$STATE"
  log "Gitea main -> $new"
else
  log "GitHub main unchanged ($GH_MAIN); overlay preserved"
fi
log "sync OK"
