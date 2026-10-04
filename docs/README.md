# Documentation index

Two audiences read this directory, and they need almost disjoint sets of files.
Find yourself below.

## You are running audr

You want a working instance and a way to keep it working. Four files, in the
order you will need them.

| Document | What it covers |
|---|---|
| [operations.md](operations.md) | **Start here.** Install, secrets, seeding, TLS, backup and restore, key loss, purge, reset, troubleshooting |
| [architecture.md](architecture.md) | What the parts are and how they fit: processes, data model, background jobs, auth, providers, and the full configuration reference |
| [api.md](api.md) | The HTTP API as implemented — every route, its auth requirement and its behaviour. Needed if you script against audr |
| [security-at-rest.md](security-at-rest.md) | What encryption at rest does and does not protect against here, and what is recommended |

Also useful: [third-party.md](third-party.md) lists the exact image and
dependency pins in a release, with licence attribution.

## You are developing audr

| Document | What it covers |
|---|---|
| [development.md](development.md) | Developer setup, every test suite and how to run it, lint/typecheck, migrations, conventions |
| [engineering-workflow.md](engineering-workflow.md) | Branches, the shared checkout and worktrees, when to self-merge vs. escalate, the QA gate, planning artifacts |
| [containers.md](containers.md) | Why each compose service exists, what was removed, what could still go |
| [releases.md](releases.md) | Semantic versioning: what each bump means here, cutting a release, image tags, verifying what is deployed |
| [deploy-runbook.md](deploy-runbook.md) | The guarded Gitea deploy pipeline, its invariants, manual recovery, registry retention |
| [github-actions.md](github-actions.md) | The public GitHub workflows: the test gate, the manual image release, GHCR and the README badges |
| [brand.md](brand.md) | Where the logo files live, how the derived assets are generated, how to use the mark |

### Verification record

Evidence that the system does what it claims. Read when you need proof rather
than instructions.

| Document | What it covers |
|---|---|
| [verification.md](verification.md) | One-command test invocations and a recorded pass/fail run log |
| [release-1-coverage.md](release-1-coverage.md) | Every release-1 requirement mapped to the test that proves it — or recorded as a gap |
| [verification-history.md](verification-history.md) | The history/reorg verification narrative |
| [benchmark.md](benchmark.md) | The warm-read latency gate and the catalog call-count report |

### Product and scope

Why the product is shaped the way it is. Background, not instructions.

| Document | What it covers |
|---|---|
| [product-vision.md](product-vision.md) | Purpose, owner experience, boundaries, measures of value |
| [roadmap.md](roadmap.md) | Release 1/2/3 scope and the backlog |
| [discovery.md](discovery.md) | Dated decision record — why the scope is what it is |

---

For the scripts next to these documents, see
[`scripts/README.md`](../scripts/README.md), which splits them the same way:
the handful an operator runs, and the rest.
