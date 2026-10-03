// Build identity of this bundle, injected by Vite's `define` (see
// ../build-meta.ts). Read through this module rather than touching the globals
// directly so there is one place to change if the injection mechanism moves.

export const APP_VERSION: string = __APP_VERSION__

/** Full commit sha the image was built from; empty in a local build. */
export const APP_COMMIT: string = __APP_COMMIT__

/** `v1.2.3` — the semantic version as it is shown to a human. */
export const VERSION_LABEL = `v${APP_VERSION}`

/**
 * Hover text for the version label: adds the short commit when CI supplied one,
 * so an operator can tell two builds of the same version apart.
 */
export const VERSION_TITLE = APP_COMMIT
  ? `audr ${VERSION_LABEL} (${APP_COMMIT.slice(0, 12)})`
  : `audr ${VERSION_LABEL}`
