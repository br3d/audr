#!/usr/bin/env python3
"""Tag-retention pruner for the audr image registry (AUD-333 item 5).

The audr image registry is a plain CNCF `distribution` registry behind nginx —
NOT Harbor. It therefore has no built-in tag-retention feature to configure:
retention has to be enforced from the outside, which is what this script is. It
talks only the /v2 HTTP API, so it needs no shell on the registry host (we do
not have one).

WHAT IT DOES NOT DO
-------------------
Deleting a manifest only unlinks it. The blobs behind it are reclaimed solely
by `registry garbage-collect -c /etc/docker/registry/config.yml`, which must run
as a process on the registry host. Until someone with a shell on that host runs
it (AUD-333 item 4), this script frees *catalog clutter*, not disk. Every
run says so at the end rather than letting the operator assume bytes came back.

RETENTION RULES (a tag survives if ANY apply)
---------------------------------------------
  * it is `latest` or `rollback` — the two aliases the deploy pipeline moves;
  * it looks like a release tag — `1.2.3` (what the pipeline publishes since
    AUD-407) or the older `v1.2.3` spelling. Ordinary per-commit builds are
    `1.2.3-g<sha>` and are NOT release tags;
  * it is the tag currently pinned in the deploy host's .env (BACKEND_TAG), or
    was passed via --protect;
  * it is one of the --keep newest tags by image creation time, in ANY of the
    pruned repositories (see "symmetry" below);
  * it shares a manifest digest with any tag protected by the rules above.

SYMMETRY ACROSS REPOSITORIES
----------------------------
Only of historical interest while DEFAULT_REPOS holds a single repository, but
the mechanism is kept because it is what makes any multi-repo run safe.

The project used to ship two images: a deploy pinned BACKEND_TAG and
FRONTEND_TAG to the *same* sha, and rollback pulled that one sha from both
audr-backend and audr-frontend. A sha therefore had to be present in both or in
neither — keeping it on one side only yielded a rollback that half-succeeded
and left the stack mismatched.

Per-repo age ranking cannot deliver that on its own: many tags here share an
identical image `created` timestamp (a rebuild of unchanged layers reuses the
date), so "newest N" hits ties that break differently in each repository. The
keep-set is therefore computed as the UNION of each repository's newest-N and
applied to every repository.

THE ORPHANED audr-frontend REPOSITORY
-------------------------------------
AUD-388 folded SPA serving into the API and deleted the `audr-frontend` image,
so it is no longer in DEFAULT_REPOS and a normal run leaves it alone. The tags
already pushed there are dead weight; clear them deliberately with
`--repo audr-frontend --keep 1 --apply` (--keep must be >= 1) rather than by
widening the default. Note that `latest` there is an alias and so survives any
--keep; deleting the repository outright is a registry-host operation.

That last rule is not a nicety. Deletion in the /v2 API is BY DIGEST, and a
digest delete removes *every* tag pointing at it — so pruning a stale sha tag
that happens to be what `latest` or `rollback` also points at would silently
destroy the rollback path.

WHY THE LIVE/PREVIOUS TAGS MUST SURVIVE
---------------------------------------
deploy.yaml's rollback() restores the previous release by `docker pull`ing the
immutable per-sha tag `audr-backend:<prev-sha>` from this registry, falling back
to `:rollback`. A host prune or rebuild is exactly when that pull is needed, so
those tags are load-bearing, not history. Deleting them disarms rollback.

Dry-run is the default. Nothing is deleted without --apply.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request


# No endpoint is baked in: this repository is public. The registry comes from
# --registry, else $AUDR_REGISTRY_URL, else $AUDR_REGISTRY (the name compose.yaml
# and deploy.env use, which is a bare host:port and so needs a scheme prefixed).
# See deploy.env.example.
def _default_registry() -> str | None:
    url = os.environ.get("AUDR_REGISTRY_URL")
    if url:
        return url
    host = os.environ.get("AUDR_REGISTRY")
    if host:
        return host if "://" in host else f"http://{host}"
    return None


# The registry is shared with an unrelated project (svetu-*). Scope every run to
# repositories we own so a typo or a future --repo flag cannot reach them.
REPO_PREFIX = "audr-"
# Single-entry since AUD-388 removed the `audr-frontend` image. See the
# "orphaned audr-frontend repository" note in the module docstring.
DEFAULT_REPOS = ["audr-backend"]

# Aliases the deploy pipeline itself moves; never prunable.
ALIAS_TAGS = {"latest", "rollback"}
# Two accepted release spellings: `v1.2.3` (git-tag spelling, and what this
# registry held before AUD-407) and `1.2.3` (what the deploy pipeline publishes
# for a release — an image tag conventionally carries no leading `v`).
#
# The bare form is deliberately STRICT — exactly three components and nothing
# after them. AUD-407 also made every ordinary build `<version>-g<sha>`, so a
# pattern loose enough to accept a suffix would mark every commit ever pushed to
# main as a release and silently turn retention off. The `v` form stays
# permissive so historical `v1`, `v1.2` and `v1.2.3-rc1` tags keep protection.
RELEASE_TAG_RE = re.compile(r"^(?:v\d+(?:\.\d+)*(?:[-+].*)?|\d+\.\d+\.\d+)$")

MANIFEST_ACCEPT = ",".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class RegistryError(RuntimeError):
    pass


def _request(registry, path, method="GET", accept=MANIFEST_ACCEPT):
    # `registry` is the operator's own --registry/$AUDR_REGISTRY_URL config, not
    # untrusted input, so a bogus file:// scheme is a self-inflicted operator error,
    # not an attacker-reachable vector.
    req = urllib.request.Request(  # noqa: S310 — operator's own --registry config, not untrusted input
        registry.rstrip("/") + path, headers={"Accept": accept}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — operator's own --registry config, not untrusted input
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except urllib.error.URLError as exc:
        raise RegistryError(f"{method} {path}: {exc.reason}") from exc


def get_json(registry, path, accept=MANIFEST_ACCEPT):
    status, _, body = _request(registry, path, accept=accept)
    if status != 200:
        raise RegistryError(f"GET {path} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def list_tags(registry, repo):
    status, _, body = _request(registry, f"/v2/{repo}/tags/list", accept="application/json")
    if status == 404:
        return []
    if status != 200:
        raise RegistryError(f"tags/list {repo} -> HTTP {status}")
    return json.loads(body).get("tags") or []


def tag_digest(registry, repo, tag):
    """The digest the tag resolves to — this is what a delete would target."""
    status, headers, _ = _request(registry, f"/v2/{repo}/manifests/{tag}", method="HEAD")
    if status != 200:
        raise RegistryError(f"HEAD {repo}:{tag} -> HTTP {status}")
    digest = headers.get("Docker-Content-Digest")
    if not digest:
        raise RegistryError(f"{repo}:{tag} returned no Docker-Content-Digest")
    return digest


def tag_created(registry, repo, tag):
    """Image creation timestamp, descending an OCI index if there is one.

    Returns None when it cannot be determined; callers must treat that as
    "unknown age" and refuse to prune on age alone.
    """
    try:
        manifest = get_json(registry, f"/v2/{repo}/manifests/{tag}")
    except RegistryError:
        return None

    if manifest.get("mediaType") in (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ):
        children = [
            child
            for child in manifest.get("manifests", [])
            # buildx attestation manifests carry platform unknown/unknown and
            # have no image config to read a date from.
            if (child.get("platform") or {}).get("architecture") not in (None, "unknown")
        ]
        if not children:
            return None
        try:
            manifest = get_json(registry, f"/v2/{repo}/manifests/{children[0]['digest']}")
        except RegistryError:
            return None

    config = (manifest.get("config") or {}).get("digest")
    if not config:
        return None
    try:
        return get_json(registry, f"/v2/{repo}/blobs/{config}", accept="*/*").get("created")
    except (RegistryError, json.JSONDecodeError):
        return None


def delete_digest(registry, repo, digest):
    status, _, body = _request(registry, f"/v2/{repo}/manifests/{digest}", method="DELETE")
    if status in (200, 202):
        return
    if status == 405:
        raise RegistryError(
            f"DELETE {repo}@{digest[:19]} -> 405: manifest deletion is disabled on this "
            "registry (set storage.delete.enabled=true in its config.yml)"
        )
    raise RegistryError(f"DELETE {repo}@{digest[:19]} -> HTTP {status}: {body[:200]!r}")


def read_env_tags(path):
    """Pull BACKEND_TAG out of the deploy host's .env.

    FRONTEND_TAG is deliberately ignored since AUD-388. Deploys no longer set
    it, but a stale line from before that change can still be sitting in the
    live .env, and honouring it would protect whatever old sha it names.
    """
    tags = set()
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.strip().partition("=")
                if key == "BACKEND_TAG" and value:
                    tags.add(value)
    except OSError:
        return None
    return tags


def survey_repo(registry, repo):
    """Collect every tag in a repository with its digest and creation time."""
    tags = list_tags(registry, repo)
    created, digests = {}, {}
    for tag in tags:
        created[tag] = tag_created(registry, repo, tag)
        try:
            digests[tag] = tag_digest(registry, repo, tag)
        except RegistryError as exc:
            print(f"  warn: {exc} — treating {repo}:{tag} as protected", file=sys.stderr)
            digests[tag] = None
    return {"tags": tags, "created": created, "digests": digests}


def base_reason(tag, survey, protected_tags):
    """Protection that does not depend on age ranking."""
    if tag in ALIAS_TAGS:
        return "alias the deploy pipeline moves"
    if RELEASE_TAG_RE.match(tag):
        return "release tag"
    if tag in protected_tags:
        return "deployed/explicitly protected"
    if survey["created"][tag] is None:
        return "creation time unknown"
    if survey["digests"][tag] is None:
        return "digest unresolvable"
    return None


def newest_tags(survey, keep, protected_tags):
    """The keep newest unprotected tags in one repository.

    Ties on `created` are broken by tag name so the choice is at least
    deterministic; cross-repo agreement is secured by unioning these sets.
    """
    candidates = [
        tag
        for tag in survey["tags"]
        if survey["created"][tag] and base_reason(tag, survey, protected_tags) is None
    ]
    candidates.sort(key=lambda tag: (survey["created"][tag], tag), reverse=True)
    return set(candidates[:keep])


def plan_repo(survey, protected_tags, keep_set, keep):
    """Decide, per tag, keep-or-prune. Returns (rows, prunable_digests)."""
    tags = survey["tags"]
    if not tags:
        return [], {}
    created, digests = survey["created"], survey["digests"]

    reasons = {tag: base_reason(tag, survey, protected_tags) for tag in tags}
    for tag in tags:
        if reasons[tag] is None and tag in keep_set:
            reasons[tag] = f"within newest {keep} (any repo)"

    # Digest solidarity: a digest delete takes every tag with it, so any digest
    # shared with a protected tag is off limits.
    protected_digests = {digests[t] for t, r in reasons.items() if r and digests[t]}
    for tag in tags:
        if reasons[tag] is None and digests[tag] in protected_digests:
            reasons[tag] = "shares a digest with a protected tag"

    rows = sorted(tags, key=lambda t: (created[t] or "", t), reverse=True)
    prunable = {}
    for tag in rows:
        if reasons[tag] is None:
            prunable.setdefault(digests[tag], []).append(tag)
    return [(t, created[t], digests[t], reasons[t]) for t in rows], prunable


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--registry",
        default=_default_registry(),
        help="registry base URL (default: $AUDR_REGISTRY_URL or $AUDR_REGISTRY)",
    )
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        metavar="REPO",
        help=f"repository to prune (default: {' '.join(DEFAULT_REPOS)})",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=8,
        metavar="N",
        help="keep the N newest otherwise-prunable tags (default: 8)",
    )
    parser.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="TAG",
        help="never prune this tag (repeatable)",
    )
    parser.add_argument(
        "--env-file",
        default="/home/codex/audr/.env",
        metavar="PATH",
        help="deploy .env to read the live BACKEND_TAG from",
    )
    parser.add_argument(
        "--apply", action="store_true", help="actually delete; without it the run is a dry run"
    )
    args = parser.parse_args(argv)

    repos = args.repos or DEFAULT_REPOS
    for repo in repos:
        if not repo.startswith(REPO_PREFIX):
            parser.error(
                f"refusing to touch {repo!r}: this registry is shared with other "
                f"projects, so only {REPO_PREFIX}* repositories may be pruned"
            )
    if args.keep < 1:
        parser.error("--keep must be at least 1")

    protected = set(args.protect) | ALIAS_TAGS
    env_tags = read_env_tags(args.env_file)
    if env_tags is None:
        # Not fatal, but the live tag is the one tag whose deletion would break
        # a rollback, so make the gap loud.
        print(
            f"WARNING: could not read {args.env_file}; the live deployed tag is NOT "
            f"protected by that source. Pass it via --protect if you are not on the "
            f"deploy host.\n",
            file=sys.stderr,
        )
    else:
        protected |= env_tags
        print(f"live tags from {args.env_file}: {', '.join(sorted(env_tags)) or '<none>'}")

    mode = "APPLY (deleting)" if args.apply else "DRY RUN (nothing will be deleted)"
    if not args.registry:
        parser.error(
            "no registry configured — pass --registry, or set AUDR_REGISTRY_URL / "
            "AUDR_REGISTRY (see deploy.env.example)"
        )

    print(f"registry: {args.registry}\nmode: {mode}\nkeep newest: {args.keep}\n")

    failures = 0
    total_pruned = 0

    # Pass 1: survey every repository, then union their newest-N sets so a sha
    # kept in one repository is kept in all of them (see "symmetry" in __doc__).
    surveys = {}
    for repo in repos:
        try:
            surveys[repo] = survey_repo(args.registry, repo)
        except RegistryError as exc:
            print(f"ERROR surveying {repo}: {exc}", file=sys.stderr)
            failures += 1
    if not surveys:
        return 1

    keep_set = set()
    for survey in surveys.values():
        keep_set |= newest_tags(survey, args.keep, protected)

    # Pass 2: decide and (optionally) delete.
    for repo, survey in surveys.items():
        print(f"=== {repo} ===")
        rows, prunable = plan_repo(survey, protected, keep_set, args.keep)

        if not rows:
            print("  no tags")
            continue

        for tag, created, digest, keep_reason in rows:
            mark = "keep " if keep_reason else "PRUNE"
            when = (created or "?")[:19]
            note = f"  ({keep_reason})" if keep_reason else ""
            print(f"  {mark} {tag:<42} {when} {(digest or '?')[:19]}{note}")

        if not prunable:
            print("  nothing to prune\n")
            continue

        # A repository with no tags left is indistinguishable from a deleted one
        # and would make the deploy pipeline's pulls fail outright.
        if len(prunable) and all(r[3] is None for r in rows):
            print(
                "  ERROR: every tag is prunable — refusing (would empty the repository)",
                file=sys.stderr,
            )
            failures += 1
            continue

        count = sum(len(t) for t in prunable.values())
        print(f"  -> {count} tag(s) across {len(prunable)} digest(s) to prune")
        if args.apply:
            for digest, tags in prunable.items():
                try:
                    delete_digest(args.registry, repo, digest)
                    print(f"     deleted {digest[:19]} ({', '.join(tags)})")
                    total_pruned += len(tags)
                except RegistryError as exc:
                    print(f"     FAILED {exc}", file=sys.stderr)
                    failures += 1
        print()

    if args.apply:
        print(f"Pruned {total_pruned} tag(s).")
        print(
            "NOTE: blobs are NOT reclaimed yet. Disk on the registry host is freed only by\n"
            "  registry garbage-collect -c /etc/docker/registry/config.yml\n"
            "run on the registry host, which we have no shell on (AUD-333 item 4)."
        )
    else:
        print("Dry run complete — re-run with --apply to delete.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
