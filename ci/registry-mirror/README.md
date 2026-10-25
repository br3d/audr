# Docker Hub pull-through cache

Operator notes for the LAN registry cache (AUD-747). The files here are the
whole definition: `compose.yaml`, `config.yml`, and `install.sh`.

## Why it exists

Base images are pinned by digest (AUD-697). That is correct, and it has a cost:
a floating tag whose digest has not moved resolves against an image already in
the local store, while a **bumped digest cannot** — by construction it names
something not yet local, so the first build after a bump must pull.

Docker Hub's anonymous pull allowance is per **source IP** over a rolling
window, not per repository or per agent. Several agents build on the build host
continuously and the Gitea runner shares the same egress, so that mandatory
pull is the one most likely to be refused:

```
Error response from daemon: unknown: failed to resolve reference
  "docker.io/library/node@sha256:fe4f6d74...":
  unexpected status from HEAD request to registry-1.docker.io: 429 Too Many Requests
```

It recurs on exactly the commits we least want red — every weekly Dependabot
`docker` bump. This cache makes one upstream pull serve the whole LAN.

## Topology

| Host | Role | Change |
| --- | --- | --- |
| `192.168.1.152` (build box, agents) | serves the cache **and** uses it | `install.sh` |
| `192.168.1.228` (deploy host) | uses it | `install.sh --client-only` |
| Gitea runner host | uses it | `install.sh --client-only` — **not yet applied**, see below |

Only `docker.io` pulls go through it. The private registry at
`192.168.1.90:8085` is unaffected: it holds `audr-backend`/`audr-frontend` and
stays the registry of record. It could not host the cache itself — a
`distribution` registry in `proxy` mode refuses pushes.

## Install / verify

```sh
./install.sh                 # serve the cache here and use it
./install.sh --client-only   # only use a cache someone else serves
./install.sh --check         # report state, change nothing
```

Idempotent, and re-running is how you verify. It merges two keys into
`/etc/docker/daemon.json` (keeping a `.bak`) and reloads dockerd with **SIGHUP**,
which is why running containers are left alone — both keys are in dockerd's
reloadable set:

```json
"registry-mirrors":    ["http://192.168.1.152:5001"],
"insecure-registries": ["192.168.1.90:8085", "192.168.1.152:5001"]
```

The mirror speaks plain HTTP, so dockerd requires the same `host:port` in
`insecure-registries` as well. ufw restricts the port to `192.168.1.0/24`: an
unrestricted pull-through cache is an open proxy for anyone who can reach it.

`--check` on a wired host:

```
==> daemon sees:   ["http://192.168.1.152:5001/"]
==> cache answers: 200
```

## Failure behaviour

A dead cache does **not** break pulls: dockerd falls back to the upstream
registry, which is exactly today's behaviour. So the worst case of the cache
being down is the rate limit we already had.

`scripts/test.sh` pre-pulls every pinned image in an `image_pull` phase before
the suite starts, with backoff, and names a 429 as a 429. That is deliberate
belt-and-braces: without it a rate limit surfaces as whichever phase first
needed the image — it was reported as `Frontend dependency install FAILED` —
and an agent reading only step conclusions (the one CI channel readable without
credentials, AUD-714) goes looking in `package-lock.json` for a fault that is
in the network.

## Upstream credentials (optional)

Anonymous is enough for the cache to work. A Docker Hub account raises the
allowance this one cache spends upstream, and needs no change to any host's
`daemon.json`:

```sh
printf 'REGISTRY_PROXY_USERNAME=…\nREGISTRY_PROXY_PASSWORD=…\n' > .env  # gitignored
docker compose -f compose.yaml up -d --force-recreate
```

The variables must be *absent*, not empty, when no account is configured —
hence the valueless `environment:` entries in `compose.yaml`.

## Disk

The cache is bounded by `proxy.ttl` (336h) in `config.yml`: content that stops
being pulled — every superseded Dependabot pin — ages out on its own. `delete`
must stay enabled for that expiry to work. To inspect or reclaim by hand:

```sh
docker exec docker-hub-mirror du -sh /var/lib/registry
docker exec docker-hub-mirror /bin/registry garbage-collect /etc/docker/registry/config.yml
```

## Remaining

The Gitea runner host is not wired up: it is not reachable from the agent
runtime with the `id_ed25519` deploy key, and it is the host where a red CI leg
hurts most. Someone with shell access there runs `install.sh --client-only`
(it needs nothing else from this directory). Tracked separately.
