// Global test setup — import shared matchers or polyfills here as needed.

// Tell React we are in a test environment so it enforces act() boundaries
;(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
