#!/usr/bin/env bash
# What the host installers pin to the deploy host, and how to tell whether the
# host still has it (AUD-698). Source, do not run:
#
#   . "$(dirname "$0")/lib/host-install-manifest.sh"
#   audr_drift_report "$abs_install" "$REMOTE_DIR" "${AUDR_BACKUP_FILES[@]}"
#
# WHY A MANIFEST AT ALL
# ---------------------
# install-backup.sh, install-watchdog.sh and install-mirror-dropin.sh each copy
# a PINNED COPY of a handful of repo files to the host, for a good reason: the
# only git checkout on that host belongs to the CI runner, which rewrites it on
# every deploy and resets it on a failed one. A watchdog or a backup whose code
# lives in that checkout would stop working at exactly the moment a bad deploy
# makes it necessary (ci/host-units/audr-dump-prune.service says the same).
#
# The cost of pinning is drift: editing scripts/backup.sh in this repo does NOT
# reach the host until the installer runs again, and docs/operations.md has
# always admitted so. What was missing is anything that NOTICES. Each installer
# has a `--check`, but until this change `--check` reported only presence and
# unit status — never whether the installed bytes still match the repo — and it
# exited 0 either way, so there was nothing a workflow could gate on.
#
# That is the same drift class as AUD-443, where the host's copies of the CI
# workflow files fell behind the repo's and nobody noticed for two hours. The
# fix there was to ship the workflows with the commit. These files cannot be
# shipped with the commit — that is the whole point of pinning them — so the
# alternative is to measure the drift and say so.
#
# THE LISTS BELOW ARE THE SOURCE OF TRUTH.
# Each installer sources this file and uses these arrays to drive both the copy
# and the check, so the two cannot disagree. Before this file they were written
# out twice per installer (once in the `scp` argument list, once in the `chmod`)
# plus a third time in prose in the header, and backend/tests/unit/
# test_host_install_drift.py now pins the installers to these arrays.
#
# ENTRY FORMAT: "<repo-relative source>|<absolute path on the host>|<mode>"
#
#   mode = verbatim   byte-for-byte copy; compare hashes directly
#   mode = render     a systemd unit template whose @INSTALL_DIR@ and
#                     @REMOTE_DIR@ placeholders the installer substitutes;
#                     compare against the LOCALLY RENDERED text, since the
#                     host's copy legitimately differs from the repo's
#
# `@INSTALL@` in a host path is substituted with the installer's resolved
# absolute install directory, which is only knowable at run time (it is under
# the remote user's $HOME).
#
# NOT HERE, on purpose: ci/host-units/audr-dump-prune.{service,timer}. Those run
# on the Paperclip host rather than the deploy host (AUD-505/AUD-509) and no
# script in this repo installs them, so there is no installer whose `--check`
# could report on them and nothing for this manifest to pair against. Wire them
# in here if that ever changes.

# install-backup.sh (AUD-562).
#
# restore.sh is shipped although no timer calls it: the moment it is needed is a
# lost db_data volume, and an operator in that position should find the restore
# tool next to the dumps rather than have to clone the repo onto a host that is
# already having a bad day. notify.sh and notify-board.py travel together
# because notify.sh resolves its sibling relative to its own directory —
# separate them and alerting silently degrades to a log line.
# Consumed by the installers that source this file. SC2034 is disabled because
# the linter analyses one file at a time and cannot see those consumers; a
# genuinely unused array would still be caught by test_host_install_drift.py,
# which asserts each installer references the one it needs.
# shellcheck disable=SC2034
AUDR_BACKUP_FILES=(
  "scripts/backup-run.sh|@INSTALL@/scripts/backup-run.sh|verbatim"
  "scripts/backup.sh|@INSTALL@/scripts/backup.sh|verbatim"
  "scripts/dump-prune.py|@INSTALL@/scripts/dump-prune.py|verbatim"
  "scripts/restore.sh|@INSTALL@/scripts/restore.sh|verbatim"
  "scripts/notify.sh|@INSTALL@/scripts/notify.sh|verbatim"
  "scripts/notify-board.py|@INSTALL@/scripts/notify-board.py|verbatim"
  "ci/host-units/audr-backup.service.in|/etc/systemd/system/audr-backup.service|render"
  "ci/host-units/audr-backup.timer|/etc/systemd/system/audr-backup.timer|verbatim"
)

# install-watchdog.sh (AUD-444, extended by AUD-584).
#
# Two timers, one installer: both are host-side delivery over the same notify.sh
# and the same secrets directory, so a second install script would only create a
# way to end up with half of it installed.
# Consumed by the installers that source this file. SC2034 is disabled because
# the linter analyses one file at a time and cannot see those consumers; a
# genuinely unused array would still be caught by test_host_install_drift.py,
# which asserts each installer references the one it needs.
# shellcheck disable=SC2034
AUDR_WATCHDOG_FILES=(
  "scripts/stand-watchdog.sh|@INSTALL@/scripts/stand-watchdog.sh|verbatim"
  "scripts/alert-dispatch.sh|@INSTALL@/scripts/alert-dispatch.sh|verbatim"
  "scripts/notify.sh|@INSTALL@/scripts/notify.sh|verbatim"
  "scripts/notify-board.py|@INSTALL@/scripts/notify-board.py|verbatim"
  "ci/host-units/audr-watchdog.service.in|/etc/systemd/system/audr-watchdog.service|render"
  "ci/host-units/audr-watchdog.timer|/etc/systemd/system/audr-watchdog.timer|verbatim"
  "ci/host-units/audr-alert-dispatch.service.in|/etc/systemd/system/audr-alert-dispatch.service|render"
  "ci/host-units/audr-alert-dispatch.timer|/etc/systemd/system/audr-alert-dispatch.timer|verbatim"
)

# install-mirror-dropin.sh. Only the drop-in, not the unit: `audr-mirror.service`
# and its timer hold the host's own layout (the mirror script path, the Gitea
# push URL's directory) and this repository is public, so the unit stays off it.
# The drop-in references nothing but `github.com` and is safe to track.
# Consumed by the installers that source this file. SC2034 is disabled because
# the linter analyses one file at a time and cannot see those consumers; a
# genuinely unused array would still be caught by test_host_install_drift.py,
# which asserts each installer references the one it needs.
# shellcheck disable=SC2034
AUDR_MIRROR_FILES=(
  "ci/host-units/audr-mirror-dns-race.conf|/etc/systemd/system/audr-mirror.service.d/10-dns-race.conf|verbatim"
)

# audr_manifest_scp_sources <repo-root> <abs-install> <entry>...
# Prints the local paths of the `verbatim` entries that live under the install
# directory — i.e. exactly the `scp` argument list. Units are excluded: they go
# in over `sudo tee` because the SSH user has no write access to /etc.
audr_manifest_scp_sources() {
  local root="${1:?repo root required}" install="${2:?install dir required}"
  shift 2
  local entry src dest mode
  for entry in "$@"; do
    IFS='|' read -r src dest mode <<<"${entry}"
    case "${dest}" in "${install}"/*|'@INSTALL@'/*) ;; *) continue ;; esac
    [ "${mode}" = verbatim ] || continue
    printf '%s\n' "${root}/${src}"
  done
}

# audr_manifest_remote_targets <abs-install> <entry>...
# Prints the host-side paths of the entries under the install directory, which
# is the `chmod 755` argument list. Same set as above, resolved remote-side.
audr_manifest_remote_targets() {
  local install="${1:?install dir required}"
  shift
  local entry src dest mode
  for entry in "$@"; do
    IFS='|' read -r src dest mode <<<"${entry}"
    case "${dest}" in '@INSTALL@'/*) ;; *) continue ;; esac
    printf '%s\n' "${dest/@INSTALL@/${install}}"
  done
}

# audr_render_unit <path> <abs-install> <remote-dir>
# The one substitution the installers apply to a `.in` unit template. Defined
# here rather than in each installer so the check renders a unit the same way
# the install wrote it — a check that rendered it differently would report
# permanent drift on a correctly installed host, which is worse than no check:
# an alert that is always firing is an alert nobody reads.
audr_render_unit() {
  sed -e "s|@INSTALL_DIR@|${2}|g" -e "s|@REMOTE_DIR@|${3}|g" "${1}"
}

# audr_drift_report <repo-root> <abs-install> <remote-dir> <entry>...
#
# Compares every manifest entry's repo content against the host's copy and
# prints one line per file. Returns 0 when every file matches, 1 when any has
# drifted or is missing.
#
# Requires the caller to have defined an `SSH` array (every installer does),
# because the connection options — key, timeout, host — are the caller's to own.
#
# Hashes rather than a diff: the content may be a 400-line python script, the
# answer that matters is one bit, and a diff of a file holding no secret today
# may hold one tomorrow. The hashes are computed with the same `sha256sum` on
# both ends, in ONE ssh round trip rather than one per file — a per-file round
# trip over the LAN made the old watchdog install take visible seconds, and this
# runs on every deploy.
# <abs-install> and <remote-dir> may be empty for a manifest whose entries are
# all absolute host paths needing no substitution (install-mirror-dropin.sh is
# the case): `${2:?}` would reject the empty string, not just an unset argument.
# An entry that DOES carry @INSTALL@ with no install dir to put there is a
# caller bug and is reported as such rather than silently checking `/scripts/x`.
audr_drift_report() {
  local root="${1:?repo root required}"
  local install="${2-}"
  local remote_dir="${3-}"
  shift 3

  local entry src dest mode local_hash remote_hash resolved
  local -a paths=()
  local drift=0 missing=0 ok=0

  # Remote side first: one `sha256sum` over every path, in one call. Paths that
  # do not exist make sha256sum complain on stderr and skip the line, which is
  # the signal we want — a missing line is a missing file.
  for entry in "$@"; do
    IFS='|' read -r src dest mode <<<"${entry}"
    if [ -z "${install}" ] && [ "${dest}" != "${dest/@INSTALL@/}" ]; then
      printf '  ERROR            %s needs an install dir and none was given\n' "${dest}"
      return 1
    fi
    paths+=("${dest/@INSTALL@/${install}}")
  done

  local remote_out
  # shellcheck disable=SC2029  # the paths must expand HERE: they are built from
  # the manifest plus the resolved install dir, and the remote shell has neither.
  remote_out="$("${SSH[@]}" "sha256sum $(printf "'%s' " "${paths[@]}") 2>/dev/null" || true)"

  for entry in "$@"; do
    IFS='|' read -r src dest mode <<<"${entry}"
    resolved="${dest/@INSTALL@/${install}}"

    if [ ! -f "${root}/${src}" ]; then
      printf '  MISSING-IN-REPO  %s (manifest names a file this checkout does not have)\n' "${src}"
      drift=1
      continue
    fi

    if [ "${mode}" = render ]; then
      local_hash="$(audr_render_unit "${root}/${src}" "${install}" "${remote_dir}" | sha256sum | cut -d' ' -f1)"
    else
      local_hash="$(sha256sum <"${root}/${src}" | cut -d' ' -f1)"
    fi

    # The remote line is `<hash>  <path>`. Match on the path, anchored, so
    # `/a/b.sh` cannot be satisfied by `/x/a/b.sh`.
    remote_hash="$(printf '%s\n' "${remote_out}" \
      | awk -v p="${resolved}" '$2 == p || $2 == "*" p { print $1; exit }')"

    if [ -z "${remote_hash}" ]; then
      printf '  ABSENT           %s\n' "${resolved}"
      missing=1
    elif [ "${remote_hash}" = "${local_hash}" ]; then
      printf '  ok               %s\n' "${resolved}"
      ok=$((ok + 1))
    else
      printf '  DRIFTED          %s\n                   host %s != repo %s (%s)\n' \
        "${resolved}" "${remote_hash:0:12}" "${local_hash:0:12}" "${src}"
      drift=1
    fi
  done

  if [ "${drift}" -eq 0 ] && [ "${missing}" -eq 0 ]; then
    printf '  => %d file(s) on the host match this checkout\n' "${ok}"
    return 0
  fi

  # Both states are reported as drift and both are non-zero, but they are named
  # differently because the actions differ: ABSENT usually means the installer
  # has never run on this host (or ran as another user, so the install dir is
  # somewhere else), while DRIFTED means it ran and has since fallen behind.
  printf '  => DRIFT: %d matching, and the files above are stale or absent.\n' "${ok}"
  printf '     The host runs its PINNED COPY, so the repo version is not what\n'
  printf '     executes. Re-run the installer to update it.\n'
  return 1
}
