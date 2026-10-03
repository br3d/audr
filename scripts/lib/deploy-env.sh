#!/usr/bin/env bash
# Shared deployment configuration for the scripts/ helpers. Source, do not run:
#
#   . "$(dirname "$0")/lib/deploy-env.sh"
#   audr_load_deploy_env "${ROOT}"
#
# Why this file exists: the repository is public, so no deployment host, SSH
# user or registry endpoint is baked into tracked files. Every script that needs
# one reads it from the environment, or from an untracked `deploy.env` at the
# repo root (copy `deploy.env.example`). Defaults, where they exist, point at
# localhost so a fresh clone does the harmless thing instead of reaching for
# somebody else's infrastructure.
#
# Precedence is environment first, file second: CI sets these as job env or
# repository variables and must not be overridden by a file that happens to be
# lying around in a persistent self-hosted checkout.

# audr_load_deploy_env <repo-root>
# Reads KEY=value lines from <repo-root>/deploy.env, exporting only the keys
# that are not already set. Missing file is not an error — the environment (or a
# script's own default) may well be enough.
audr_load_deploy_env() {
  local root="${1:?audr_load_deploy_env: repo root required}"
  local file="${AUDR_DEPLOY_ENV:-${root}/deploy.env}"
  [ -f "${file}" ] || return 0

  local line key value
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in ''|'#'*) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    # Tolerate `export KEY=value` and surrounding whitespace, since this file is
    # written by hand.
    key="${key#export }"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    case "${key}" in *[!A-Za-z0-9_]*|'') continue ;; esac
    # Strip one layer of matching quotes; anything fancier belongs in the
    # environment, not in this file.
    case "${value}" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    [ -n "${!key+set}" ] && continue
    export "${key}=${value}"
  done < "${file}"
}

# audr_require <VAR> [hint]
# Fails with an actionable message rather than letting a script run against an
# empty host or registry — a deploy aimed at "" is the failure mode this guards.
audr_require() {
  local name="${1:?audr_require: variable name required}"
  local hint="${2:-}"
  if [ -z "${!name:-}" ]; then
    echo "ERROR: ${name} is not set." >&2
    echo "       Set it in the environment or in deploy.env (see deploy.env.example)." >&2
    [ -n "${hint}" ] && echo "       ${hint}" >&2
    return 1
  fi
}
