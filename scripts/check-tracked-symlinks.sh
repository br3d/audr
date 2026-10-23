#!/usr/bin/env bash
# Reject tracked symlinks whose target escapes the worktree (AUD-711).
#
# This exists because `frontend/node_modules` was once committed as a symlink to
# `/home/codex/git/audr/frontend/node_modules` — an absolute path that exists on
# exactly one machine. audr is a public repository, so every clone elsewhere
# materialised a dangling link into a stranger's home directory. It arrived via
# a merge and nothing caught it.
#
# It is not a hypothetical, and this is not only a hygiene gate — it is a
# regression test for a known red. `6c212b11` is the one commit that carried
# that symlink, and GitHub run 190 on it is red; the symlink is absent from the
# five neighbouring trees either side (`3b2d3244`, `1c80b26e`, `d6773b56`,
# `d50a2576`, `3fdddac`), and run 191 on `1c80b26e`, which untracked it, is
# green. On a GitHub runner that absolute target does not exist, so the checkout
# lands a dangling link exactly where the frontend suite installs into.
#
# So: a tracked symlink escaping the worktree has already cost this project one
# red CI run that took three agents to explain. That is what this check buys.
#
# `1c80b26` closed the accident path (`.gitignore` said `node_modules/`, and a
# trailing slash matches directories only, so a *symlink* of that name slipped
# past a plain `git add .`). This is the belt to that pair of braces: a
# `git add -f`, or another merge, can still reintroduce one anywhere in the tree.
#
# Rejected:
#   * any absolute target — machine-specific by construction, even on the
#     improbable chance it resolves inside this repo on this host;
#   * any relative target that climbs out of the repository root.
#
# Allowed: relative targets that stay inside the repo. Those are legitimate and
# must keep passing.
#
# Runs in CI (both pipelines), not pytest: the backend test container
# bind-mounts only ./backend plus a hand-picked file list and has no `.git`, so
# `git ls-files` would have nothing to read and the test could only skip — an
# unrun test that reads as coverage while providing none.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
ROOT="$(realpath "${ROOT}")"

checked=0
bad=0

# `git ls-files -s` emits `<mode> <sha> <stage>\t<path>`; mode 120000 is a
# symlink. NUL-delimited so paths containing spaces or newlines survive, and so
# git does not apply its own quoting to unusual names.
while IFS= read -r -d '' entry; do
  mode="${entry%% *}"
  [[ "${mode}" == "120000" ]] || continue
  path="${entry#*$'\t'}"
  checked=$(( checked + 1 ))

  # The blob *is* the target string. Read it from the index rather than the
  # filesystem: a target that escapes the worktree is usually dangling, so
  # `readlink -f` would resolve nothing.
  target="$(git cat-file -p ":${path}")"

  if [[ "${target}" == /* ]]; then
    echo "::error file=${path}::tracked symlink has an absolute target: ${target}"
    bad=$(( bad + 1 ))
    continue
  fi

  # `realpath -m` normalises `..` logically, without requiring the path to
  # exist — which is the whole point here.
  resolved="$(realpath -m "${ROOT}/$(dirname "${path}")/${target}")"
  case "${resolved}" in
    "${ROOT}" | "${ROOT}"/*) ;;
    *)
      echo "::error file=${path}::tracked symlink escapes the worktree: ${target} -> ${resolved}"
      bad=$(( bad + 1 ))
      ;;
  esac
done < <(git ls-files -s -z)

if (( bad > 0 )); then
  echo "FAIL: ${bad} of ${checked} tracked symlink(s) point outside the worktree."
  echo "A tracked symlink must use a relative target that stays inside the repository."
  exit 1
fi

echo "OK: ${checked} tracked symlink(s), all targets relative and inside the worktree."
