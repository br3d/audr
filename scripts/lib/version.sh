#!/usr/bin/env bash
# Shared semantic-version helpers (AUD-407). Source, do not run:
#
#   . "$(dirname "$0")/lib/version.sh"
#   version="$(audr_version "${ROOT}")"
#
# The repo-root VERSION file is the source of truth for scripts. The same number
# is mirrored into backend/pyproject.toml, backend/src/audr/version.py and
# frontend/package.json; scripts/release.sh rewrites all four together and
# backend/tests/test_version.py fails the build if they drift.

# audr_version <repo-root>
# Prints the current semantic version, e.g. `1.4.2`. No leading `v` — callers
# that want the tag spelling add it, so there is exactly one place that decides
# where the `v` goes.
audr_version() {
  local root="${1:?audr_version: repo root required}"
  local file="${root}/VERSION"
  if [ ! -f "${file}" ]; then
    echo "FATAL: ${file} not found — cannot determine the release version." >&2
    return 1
  fi
  local version
  version="$(tr -d ' \t\r\n' < "${file}")"
  audr_version_validate "${version}" || return 1
  printf '%s\n' "${version}"
}

# audr_version_validate <version>
# Accepts MAJOR.MINOR.PATCH with the optional prerelease/build suffixes semver
# 2.0.0 allows (e.g. `1.4.2`, `1.5.0-rc.1`). Rejects a leading `v`: tags carry
# it, the version itself does not.
audr_version_validate() {
  local version="${1:-}"
  local core='(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
  local pre='(-[0-9A-Za-z.-]+)?'
  local build='(\+[0-9A-Za-z.-]+)?'
  if ! printf '%s' "${version}" | grep -Eq "^${core}${pre}${build}$"; then
    echo "FATAL: '${version}' is not a semantic version (expected MAJOR.MINOR.PATCH, no leading 'v')." >&2
    return 1
  fi
}

# audr_image_tag <repo-root> [git-sha]
# Prints the immutable image tag for the current tree: `<version>-g<short sha>`,
# plus `-dirty` when the working tree has uncommitted changes.
#
# Why not the bare version: two builds of 1.4.2 from different commits must not
# collide in the registry, and between releases every commit on main is still
# "1.4.2" by the VERSION file. The version prefix makes a tag readable at a
# glance; the sha suffix keeps it unique and reproducible. A released tag
# `v1.4.2` additionally gets the clean `1.4.2` tag pushed alongside it — that
# one is only ever produced from the release tag, so it stays immutable too.
audr_image_tag() {
  local root="${1:?audr_image_tag: repo root required}"
  local sha="${2:-}"
  local version
  version="$(audr_version "${root}")" || return 1
  if [ -z "${sha}" ]; then
    sha="$(git -C "${root}" rev-parse --short=12 HEAD)"
  fi
  local suffix=""
  if ! git -C "${root}" diff --quiet HEAD -- 2>/dev/null; then
    suffix="-dirty"
  fi
  printf '%s-g%s%s\n' "${version}" "${sha}" "${suffix}"
}
