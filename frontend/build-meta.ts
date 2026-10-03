// Build metadata baked into the SPA bundle (AUD-407).
//
// The version shown in the sidebar is a compile-time constant rather than an
// API call, so it is correct before login, offline, and during an API outage —
// exactly the situations where someone needs to know which build they are
// looking at.
//
// package.json is the source here, not the repo-root VERSION file: the
// Dockerfile's frontend-builder stage copies only `frontend/`, so VERSION is not
// on disk during the image build. scripts/release.sh rewrites both together and
// backend/tests/test_version.py fails the build if they drift.
//
// Lives in its own module because vite.config.ts and vitest.config.ts both need
// the same `define` block — duplicating it is how the two silently diverge and
// the test suite starts asserting against a different version than ships.
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL('./package.json', import.meta.url)), 'utf8'),
) as { version: string }

export const buildDefine = {
  __APP_VERSION__: JSON.stringify(process.env.AUDR_VERSION || pkg.version),
  // Injected by CI through a Docker build arg; empty in a local build.
  __APP_COMMIT__: JSON.stringify(process.env.AUDR_GIT_SHA || ''),
}
