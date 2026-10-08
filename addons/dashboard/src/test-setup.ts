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
