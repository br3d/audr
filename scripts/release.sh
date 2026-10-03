#!/usr/bin/env bash
# Cut a semantic-version release (AUD-407).
#
# Usage:
#   ./scripts/release.sh major|minor|patch [--push]
#   ./scripts/release.sh --set 1.4.2      [--push]
#   ./scripts/release.sh --show
#
# What it does, in order:
#   1. refuses to run unless the tree is clean and HEAD is on main
#   2. rewrites the version in all four files that carry it
#   3. commits that as "Release vX.Y.Z"
#   4. creates the annotated tag vX.Y.Z
#   5. with --push: pushes main and the tag, which is what triggers the guarded
#      build+deploy pipeline (ci/gitea-overlay/workflows/deploy.yaml reacts to
#      `v*` tags). Without --push nothing leaves the machine and the script
#      prints the push command for you to run.
#
# Pushing is opt-in because a pushed tag deploys. Everything before that step is
# local and reversible with `git tag -d` + `git reset --hard HEAD~1`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/version.sh
. "${ROOT}/scripts/lib/version.sh"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

BUMP=""
EXPLICIT=""
PUSH=0
SHOW=0

while [ $# -gt 0 ]; do
  case "$1" in
    major|minor|patch) BUMP="$1"; shift ;;
    --set) EXPLICIT="${2:?--set needs a version}"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --show) SHOW=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "FATAL: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

CURRENT="$(audr_version "${ROOT}")"

if [ "${SHOW}" -eq 1 ]; then
  printf '%s\n' "${CURRENT}"
  exit 0
fi

if [ -n "${BUMP}" ] && [ -n "${EXPLICIT}" ]; then
  echo "FATAL: pass either a bump level or --set, not both." >&2
  exit 2
fi
if [ -z "${BUMP}" ] && [ -z "${EXPLICIT}" ]; then
  echo "FATAL: nothing to do — pass major|minor|patch, --set X.Y.Z, or --show." >&2
  usage >&2
  exit 2
fi

# ---- Compute the next version -------------------------------------------

if [ -n "${EXPLICIT}" ]; then
  audr_version_validate "${EXPLICIT}"
  NEXT="${EXPLICIT}"
else
  # Bump off the release core only: a prerelease suffix on CURRENT is dropped,
  # which is the usual semver reading of "1.5.0-rc.1 patch-bumped" as 1.5.1 off
  # the 1.5.0 line rather than an incomprehensible 1.5.0-rc.2.
  CORE="${CURRENT%%-*}"; CORE="${CORE%%+*}"
  IFS='.' read -r MA MI PA <<<"${CORE}"
  case "${BUMP}" in
    major) NEXT="$((MA + 1)).0.0" ;;
    minor) NEXT="${MA}.$((MI + 1)).0" ;;
    patch) NEXT="${MA}.${MI}.$((PA + 1))" ;;
  esac
fi

TAG="v${NEXT}"
echo "==> ${CURRENT} -> ${NEXT} (tag ${TAG})"

# ---- Preflight ----------------------------------------------------------

BRANCH="$(git -C "${ROOT}" rev-parse --abbrev-ref HEAD)"
if [ "${BRANCH}" != "main" ] && [ "${AUDR_RELEASE_ALLOW_BRANCH:-0}" != "1" ]; then
  echo "FATAL: releases are cut from main, not '${BRANCH}'." >&2
  echo "       Merge first, or set AUDR_RELEASE_ALLOW_BRANCH=1 to override." >&2
  exit 1
fi
if [ -n "$(git -C "${ROOT}" status --porcelain)" ]; then
  echo "FATAL: working tree is dirty — commit or stash before releasing." >&2
  git -C "${ROOT}" status --short >&2
  exit 1
fi
if git -C "${ROOT}" rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  echo "FATAL: tag ${TAG} already exists." >&2
  exit 1
fi

# ---- Rewrite the four version carriers ----------------------------------
#
# Each edit is anchored so it cannot touch a dependency pin that happens to
# share the number: pyproject's `version = ` only in the [project] table,
# package.json's only at top level, version.py's only the `__version__` line.

printf '%s\n' "${NEXT}" > "${ROOT}/VERSION"

python3 - "${ROOT}" "${CURRENT}" "${NEXT}" <<'PY'
import pathlib
import re
import sys

root, current, nxt = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]


def sub_once(path: pathlib.Path, pattern: str, repl: str) -> None:
    text = path.read_text()
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"FATAL: no version line matched in {path} (pattern {pattern!r})")
    path.write_text(new)


# [project] version — the first `version = "..."` in pyproject is the project's;
# dependency pins use `==` inside the dependencies list, never this spelling.
sub_once(
    root / "backend/pyproject.toml",
    r'^version = "[^"]+"$',
    f'version = "{nxt}"',
)
sub_once(
    root / "backend/src/audr/version.py",
    r'^__version__ = "[^"]+"$',
    f'__version__ = "{nxt}"',
)
# Top-level "version" in package.json: it sits at indent 2, dependency versions
# sit at indent 4 inside their own objects.
sub_once(
    root / "frontend/package.json",
    r'^  "version": "[^"]+",$',
    f'  "version": "{nxt}",',
)
# package-lock mirrors package.json's version in two places: the top-level
# field and the `""` (root package) entry under "packages". BOTH must move —
# `npm ci` aborts with "package.json and package-lock.json are not in sync" if
# the root entry disagrees with the manifest, and the Dockerfile's
# frontend-builder stage runs `npm ci`, so getting this wrong breaks every
# release build rather than just looking untidy.
#
# Edited textually rather than via json.load/json.dump: reserializing a
# thousand-line lock would rewrite formatting npm owns and produce a diff
# nobody can review. The `""` entry is located by anchoring on the line above
# it, because its indentation (6 spaces) is shared with every dependency's
# own "version" field.
lock = root / "frontend/package-lock.json"
if lock.exists():
    text = lock.read_text()
    changed = 0

    top, n = re.subn(
        r'^(  "version": )"' + re.escape(current) + r'"',
        lambda m: m.group(1) + f'"{nxt}"',
        text,
        count=1,
        flags=re.M,
    )
    text, changed = top, changed + n

    # `"": {` opens the root package entry; take the first "version" after it.
    anchor = re.search(r'^\s*"": \{$', text, flags=re.M)
    if anchor:
        head, tail = text[: anchor.end()], text[anchor.end() :]
        tail, n = re.subn(
            r'^(\s*"version": )"' + re.escape(current) + r'"',
            lambda m: m.group(1) + f'"{nxt}"',
            tail,
            count=1,
            flags=re.M,
        )
        text, changed = head + tail, changed + n

    if changed != 2:
        sys.exit(
            f"FATAL: expected to rewrite 2 version fields in {lock}, rewrote {changed}. "
            "`npm ci` would reject the lock — fix it by hand or re-run `npm install`."
        )
    lock.write_text(text)
PY

echo "==> version files updated:"
git -C "${ROOT}" --no-pager diff --stat

# ---- Commit and tag -----------------------------------------------------

git -C "${ROOT}" add VERSION backend/pyproject.toml backend/src/audr/version.py \
  frontend/package.json frontend/package-lock.json
git -C "${ROOT}" commit -m "Release ${TAG}"
git -C "${ROOT}" tag -a "${TAG}" -m "audr ${TAG}"
echo "==> committed and tagged ${TAG}"

if [ "${PUSH}" -eq 1 ]; then
  echo "==> pushing main and ${TAG} (this triggers the guarded deploy)"
  git -C "${ROOT}" push origin main
  git -C "${ROOT}" push origin "${TAG}"
  echo "==> released ${TAG}"
else
  cat <<EOF

Nothing has been pushed. To publish the release (and trigger the deploy):

  git push origin main && git push origin ${TAG}

To undo locally:

  git tag -d ${TAG} && git reset --hard HEAD~1
EOF
fi
