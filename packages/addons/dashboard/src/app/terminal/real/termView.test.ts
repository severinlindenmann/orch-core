import { afterEach, expect, it, vi } from 'vitest'
import { createTermView } from './termView'

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers() })

it('disposes immediately with open and write work pending, without scheduling more frames', () => {
  vi.useFakeTimers()
  const ran = vi.fn()
  const request = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) =>
    window.setTimeout(() => { ran(); cb(performance.now()) }, 16))
  const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((id) => window.clearTimeout(id))
  const host = document.createElement('div')
  document.body.append(host)
  const view = createTermView(host)
  const dispose = vi.spyOn(view.term, 'dispose')
  view.output(0, new TextEncoder().encode('pending output\r\n'.repeat(100)))
  view.dispose()
  expect(dispose).toHaveBeenCalledTimes(1)
  const requests = request.mock.calls.length
  vi.advanceTimersByTime(1000)
  expect(request).toHaveBeenCalledTimes(requests)
  expect(cancel).toHaveBeenCalled()
  expect(ran).not.toHaveBeenCalled()
  // Stale socket/observer callbacks must not revive a disposed view.
  const write = vi.spyOn(view.term, 'write')
  view.output(0, new Uint8Array([65]))
  view.gap(0, 1)
  view.setWriter(true)
  view.resize(100, 30)
  expect(view.fit()).toBeNull()
  view.dispose()
  expect(write).not.toHaveBeenCalled()
  expect(dispose).toHaveBeenCalledTimes(1)
  host.remove()
})
