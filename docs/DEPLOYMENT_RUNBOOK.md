# Deployment Runbook

_One page. How code ships, how to promote to production, how to roll back._

## Environments

| Env        | Host             | Deploys when                          | Health URL              |
| ---------- | ---------------- | ------------------------------------- | ----------------------- |
| Staging    | Render (Starter) | Every merge to `main` (automatic)     | `https://<staging>/health` |
| Production | Render (Starter) | Manual "Promote to Production" action | `https://<prod>/health`    |

Cost ≈ **$14/mo** (two Starter web services). No Kubernetes.

## Pipeline at a glance

```
PR opened ──▶ CI (lint · test · build)  ──▶ merge blocked until green
merge to main ──▶ Deploy Staging workflow (re-runs CI, then deploys staging, waits for /health=200)
manual trigger ──▶ Promote to Production workflow (deploys prod, waits for /health=200)
```

## Normal release flow

1. Open a PR → **CI** runs automatically (lint, test, build). Merge is gated on green.
2. Merge to `main` → **Deploy Staging** runs, deploys to staging, and verifies `/health` returns 200.
3. QA/soak on staging.

## Promote staging → production (one action)

1. GitHub → **Actions** tab → **Promote to Production** → **Run workflow**.
2. In the `confirm` field type `promote`, then **Run workflow**.
3. If a `production` environment reviewer is configured, approve the run.
4. The workflow triggers the Render prod deploy hook and waits for prod `/health` = 200.

> Production has `autoDeploy: false` — it only moves via this action, so `main` is never live until someone promotes it.

## Rollback

**Fastest (Render dashboard, ~1 min):**

1. Render dashboard → `saas-app-prod` service → **Events** / **Deploys** tab.
2. Find the last known-good deploy → **Rollback** (or **Redeploy**).
3. Confirm; wait for `/health` = 200.

**Via git (if you must re-run the pipeline):**

1. `git revert <bad-sha>` (or `git revert -m 1 <merge-sha>`) and merge the revert to `main`.
2. That auto-deploys the reverted code to **staging**.
3. Verify staging, then run **Promote to Production** to push the fix live.

Staging rolls back the same way from the `saas-app-staging` service.

## Secrets

- Real values live **only** in the Render dashboard env vars (or an env group) — never in git, never in logs.
- `render.yaml` marks `DATABASE_URL` / `SESSION_SECRET` as `sync: false` (dashboard-managed).
- GitHub repo **Secrets**: `RENDER_STAGING_DEPLOY_HOOK`, `RENDER_PROD_DEPLOY_HOOK` (signed deploy-hook URLs).
- GitHub repo **Variables**: `STAGING_HEALTH_URL`, `PROD_HEALTH_URL` (for the post-deploy health check).
- App logs are structured JSON and contain no secret values.

## One-time setup (bootstrapping the pipeline)

1. Push this repo to GitHub; set `main` as default and enable branch protection requiring the **CI** check.
2. Render → **New → Blueprint** → point at this repo. It reads `render.yaml` and creates both services.
3. Set `DATABASE_URL` / `SESSION_SECRET` on each service in the Render dashboard.
4. Copy each service's **Deploy Hook** URL → GitHub repo Secrets (`RENDER_STAGING_DEPLOY_HOOK`, `RENDER_PROD_DEPLOY_HOOK`).
5. Add repo Variables `STAGING_HEALTH_URL` / `PROD_HEALTH_URL` = each service's `/health` URL.
6. (Recommended) Settings → Environments → `production` → add required reviewers for a human approval gate.

## On-call quick checks

- Is it up? `curl -s https://<env>/health` → expect `{"status":"ok",...}`.
- What's live? The `version` field in `/health` is the deployed git SHA.
- Deploy failing? Check the GitHub Actions run, then the Render service **Logs**.
