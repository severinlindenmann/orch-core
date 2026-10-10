import { act, render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FrameNode, QUIET_MS, rememberedFit } from './FrameNode'

// The message path of a fitted frame, end to end: only `{orch: 'size', height}` from this frame's own window counts,
// the frame's border is added, and the iframe's height follows.
const node = { type: 'frame' as const, title: 'Sandboxed preview', html: '<p>x</p>', height: 280 }

function setup() {
  const r = render(<FrameNode node={node} fallback={<p>gone</p>} fitContent />)
  const frame = r.container.querySelector('iframe')!
  // jsdom does not lay out: give the frame a 1 px border top and bottom, as the `border` class does in a browser.
  Object.defineProperty(frame, 'offsetHeight', { configurable: true, get: () => parseInt(frame.style.height) || 280 })
  Object.defineProperty(frame, 'clientHeight', { configurable: true, get: () => (parseInt(frame.style.height) || 280) - 2 })
  const post = async (data: unknown, source: unknown = frame.contentWindow) => {
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', { data, source: source as Window }))
      await new Promise((ok) => requestAnimationFrame(() => ok(null)))
    })
  }
  return { frame, post }
}

describe('SandboxFrame: size messages', () => {
  it('fits to the reported height plus the border, from its own window only', async () => {
    const { frame, post } = setup()
    expect(frame.style.height).toBe('280px')
    expect(frame.contentWindow).toBeTruthy()
    await post({ orch: 'size', height: 174 })
    expect(frame.style.height).toBe('176px')
    await post({ orch: 'size', height: 100 }, window) // another window
    await post({ orch: 'other', height: 100 })
    await post({ orch: 'size', height: '100' })
    await post({ orch: 'size', height: Number.NaN })
    expect(frame.style.height).toBe('176px')
    await post({ orch: 'size', height: 5000 })
    expect(frame.style.height).toBe('280px') // never past node.height
  })

  it('a document that keeps answering its own shrink is stopped, and stays stopped after the quiet spell', async () => {
    const { frame, post } = setup()
    // A viewport-sized document 40 px short of it: every shrink of the frame brings a 40 px smaller report.
    for (let i = 0; i < 12; i++) await post({ orch: 'size', height: parseInt(frame.style.height) - 2 - 40 })
    await act(() => new Promise((ok) => setTimeout(ok, QUIET_MS + 60)))
    for (let i = 0; i < 4; i++) await post({ orch: 'size', height: parseInt(frame.style.height) - 2 - 40 })
    await act(() => new Promise((ok) => setTimeout(ok, QUIET_MS + 60)))
    expect(parseInt(frame.style.height)).toBeGreaterThanOrEqual(200)
  })
})

describe('SandboxFrame: remembered height (G4)', () => {
  it('opens a document it has fitted before at that height, not at node.height', async () => {
    const first = setup()
    expect(first.frame.style.height).toBe('280px')
    await first.post({ orch: 'size', height: 174 })
    expect(first.frame.style.height).toBe('176px')
    expect(rememberedFit(`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:">${node.html}`)).toBe(176)
    // The same document on another visit: its first frame already has the fitted height.
    const again = render(<FrameNode node={node} fallback={<p>gone</p>} fitContent />)
    expect(again.container.querySelector('iframe')!.style.height).toBe('176px')
    // Another document starts at node.height as before.
    const other = render(<FrameNode node={{ ...node, html: '<p>y</p>' }} fallback={<p>gone</p>} fitContent />)
    expect(other.container.querySelector('iframe')!.style.height).toBe('280px')
  })

  it('a frame that is not fitted ignores the memory', async () => {
    const first = setup()
    await first.post({ orch: 'size', height: 174 })
    const plain = render(<FrameNode node={node} fallback={<p>gone</p>} />)
    expect(plain.container.querySelector('iframe')!.style.height).toBe('280px')
  })
})
