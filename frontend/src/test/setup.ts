// Global test setup — import shared matchers or polyfills here as needed.

// Tell React we are in a test environment so it enforces act() boundaries
;(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true

// jsdom has no layout engine, so Recharts' <ResponsiveContainer> measures a
// 0x0 box and renders no SVG content. Give it a ResizeObserver and a
// non-zero bounding rect so chart components actually render their markup
// in tests instead of silently producing an empty container.
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as unknown as { ResizeObserver: typeof FakeResizeObserver }).ResizeObserver ??=
  FakeResizeObserver

Element.prototype.getBoundingClientRect = function getBoundingClientRect() {
  return {
    width: 600,
    height: 300,
    top: 0,
    left: 0,
    bottom: 300,
    right: 600,
    x: 0,
    y: 0,
    toJSON() {
      return this
    },
  }
}
