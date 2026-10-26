import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/react'

// Times are shown in the browser's local zone (src/lib/time.ts); tests assert them in UTC unless a test sets its own TZ.
process.env.TZ = 'UTC'

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
// Tests run with reduced motion: no animation to wait for (src/lib/motion.ts reads this query).
window.matchMedia ??= ((query: string) => ({
  matches: query.includes('prefers-reduced-motion: reduce'),
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

// jsdom logs "not implemented" for scrollTo; the app calls it on navigation.
window.scrollTo = (() => {}) as typeof window.scrollTo

// Fitted frames remember their last height for the page load (G4): each test starts without that memory.
import { afterEach } from 'vitest'
import { forgetFits } from '@/addon-ui/FrameNode'
afterEach(() => forgetFits())

// sonner removes a dismissed toast after a 200 ms timer that unmount does not clear; if it fires after the test
// environment is torn down it throws "window is not defined". Files that showed a toast dismiss everything and let
// those timers run before teardown (files without toasts pay nothing).
import { afterAll } from 'vitest'
import { toast } from 'sonner'
afterAll(async () => {
  if (toast.getHistory().length === 0) return
  toast.dismiss()
  await new Promise((resolve) => setTimeout(resolve, 300))
})
