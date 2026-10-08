import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/react'

// jsdom lacks these; cmdk, radix and recharts use them.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
Element.prototype.scrollIntoView ??= () => {}

// Cold first renders (route chunks, jsdom warm-up) can exceed the 1 s default when the machine is loaded.
configure({ asyncUtilTimeout: 4000 })

// xterm.js asks the window for matchMedia (device pixel ratio changes); jsdom has none.
window.matchMedia ??= ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
  dispatchEvent: () => false,
})) as typeof window.matchMedia

// xterm.js probes canvas support; jsdom has none and logs "not implemented" unless told it is unsupported.
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext
