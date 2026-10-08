import '@testing-library/jest-dom/vitest'

// jsdom lacks these; cmdk, radix and recharts use them.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
Element.prototype.scrollIntoView ??= () => {}
