# saas-app

Lean SaaS server scaffold with a production-ready CI/CD pipeline.

## Quick start

```bash
npm ci
cp .env.example .env
npm run dev          # http://localhost:3000
curl localhost:3000/health
```

## Scripts

| Script          | What it does                              |
| --------------- | ----------------------------------------- |
| `npm start`     | Run the server                            |
| `npm run dev`   | Run with file watching                    |
| `npm run lint`  | ESLint                                    |
| `npm test`      | Node built-in test runner                 |
| `npm run build` | Production-readiness check (require graph + entrypoint) |

## CI/CD

- **CI** (`.github/workflows/ci.yml`): lint · test · build on every PR.
- **CD staging** (`.github/workflows/deploy-staging.yml`): merge to `main` → auto-deploy to staging + health check.
- **Promote prod** (`.github/workflows/promote-production.yml`): one manual action to ship staging → production.
- Hosting: **Render** blueprint (`render.yaml`), staging + prod, ≈ $14/mo, no Kubernetes.

See [`docs/DEPLOYMENT_RUNBOOK.md`](docs/DEPLOYMENT_RUNBOOK.md) for promote/rollback and one-time setup.

## Health check

`GET /health` → `200` with `{ status, service, environment, version, uptimeSeconds }`.
