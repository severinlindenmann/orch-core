import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Collapse } from '@/components/Collapse'
import { Num } from '@/components/Num'
import { collapseOut, DURATION, fadeIn, flip, noteRect, prefersReducedMotion, takeNoted } from './motion'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const realMatchMedia = window.matchMedia
/** Motion allowed (the suite default is reduced), with a recording Element.animate. */
function allowMotion() {
  window.matchMedia = ((q: string) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  const animate = vi.fn(() => ({ finished: Promise.resolve(), cancel() {} }) as unknown as Animation)
  Element.prototype.animate = animate as unknown as typeof Element.prototype.animate
  return animate
}
afterEach(() => {
  window.matchMedia = realMatchMedia
  delete (Element.prototype as { animate?: unknown }).animate
})

const rect = (left: number, top: number) => ({ left, top, right: left + 10, bottom: top + 10, width: 10, height: 10, x: left, y: top, toJSON() {} }) as DOMRect

describe('motion helpers', () => {
  it('the test suite runs with reduced motion', () => {
    expect(prefersReducedMotion()).toBe(true)
  })

  it('under reduced motion no transform animation runs, even when the browser can animate', () => {
    const animate = vi.fn()
    Element.prototype.animate = animate as unknown as typeof Element.prototype.animate
    const el = document.createElement('div')
    expect(flip(el, { left: 100, top: 100 })).toBeNull()
    expect(fadeIn(el)).toBeNull()
    const done = vi.fn()
    collapseOut(el, done)
    expect(done).toHaveBeenCalledOnce()
    noteRect('K-1', { left: 1, top: 1 }, 'mount')
    expect(takeNoted('K-1', 'any')).toBeNull()
    expect(animate).not.toHaveBeenCalled()
  })

  it('flip plays from the old place to none, at most 200 ms, ease-out', () => {
    const animate = allowMotion()
    const el = document.createElement('div')
    el.getBoundingClientRect = () => rect(10, 20)
    flip(el, { left: 110, top: 220 }, { scaleFrom: 1.02, duration: 900 })
    const [frames, opts] = animate.mock.calls[0] as unknown as [Keyframe[], KeyframeAnimationOptions]
    expect(frames[0].transform).toBe('translate(100px, 200px) scale(1.02)')
    expect(frames[1].transform).toBe('none')
    expect(opts.duration).toBeLessThanOrEqual(200)
    expect(String(opts.easing)).toContain('cubic-bezier')
  })

  it('flip does nothing for an element that did not move', () => {
    const animate = allowMotion()
    const el = document.createElement('div')
    el.getBoundingClientRect = () => rect(10, 20)
    expect(flip(el, { left: 10, top: 20 })).toBeNull()
    expect(animate).not.toHaveBeenCalled()
  })

  it('a noted rect is taken once, of the right kind', () => {
    allowMotion()
    noteRect('K-2', { left: 5, top: 6 }, 'mount', 1.02)
    expect(takeNoted('K-2', 'settle')).toBeNull()
    expect(takeNoted('K-2', 'mount')).toMatchObject({ left: 5, top: 6, scaleFrom: 1.02 })
    expect(takeNoted('K-2', 'any')).toBeNull()
  })

  it('collapseOut animates height and opacity, then calls done', async () => {
    const animate = allowMotion()
    const el = document.createElement('li')
    const done = vi.fn()
    collapseOut(el, done)
    const opts = (animate.mock.calls[0] as unknown as [Keyframe[], KeyframeAnimationOptions])[1]
    expect(opts.duration).toBe(DURATION.base)
    await Promise.resolve()
    await Promise.resolve()
    expect(done).toHaveBeenCalledOnce()
  })

  it('every motion duration in tokens.css is at most 200 ms and the reduce block exists', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/styles/tokens.css'), 'utf8')
    const motion = css.slice(css.indexOf('Motion. Calm'))
    for (const m of motion.matchAll(/(\d+)ms/g)) expect(Number(m[1])).toBeLessThanOrEqual(200)
    expect(motion).toContain('prefers-reduced-motion: reduce')
    // Lift / slide transforms are only declared for people who allow motion.
    expect(motion.indexOf('.orch-lift')).toBeGreaterThan(motion.indexOf('prefers-reduced-motion: no-preference'))
    expect(motion.indexOf('.orch-lift')).toBeLessThan(motion.indexOf('prefers-reduced-motion: reduce'))
  })
})

describe('Collapse', () => {
  it('closed content is not rendered; opening and closing are immediate under reduced motion', () => {
    const { rerender } = render(<Collapse open={false} id="p">content</Collapse>)
    expect(screen.queryByText('content')).not.toBeInTheDocument()
    rerender(<Collapse open id="p">content</Collapse>)
    expect(screen.getByText('content')).toBeInTheDocument()
    expect(document.getElementById('p')).not.toBeNull()
    rerender(<Collapse open={false} id="p">content</Collapse>)
    expect(screen.queryByText('content')).not.toBeInTheDocument()
  })

  it('with motion allowed it opens through 0fr and unmounts once the close transition ends', () => {
    allowMotion()
    vi.useFakeTimers()
    try {
      const { rerender } = render(<Collapse open={false} id="p">content</Collapse>)
      rerender(<Collapse open id="p">content</Collapse>)
      expect(document.getElementById('p')!.style.gridTemplateRows).toBe('0fr')
      act(() => void vi.advanceTimersByTime(50))
      expect(document.getElementById('p')!.style.gridTemplateRows).toBe('1fr')
      rerender(<Collapse open={false} id="p">content</Collapse>)
      expect(screen.getByText('content')).toBeInTheDocument()
      act(() => void vi.advanceTimersByTime(300))
      expect(screen.queryByText('content')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('Num', () => {
  it('shows the value at once under reduced motion, with no animation', () => {
    const animate = vi.fn()
    Element.prototype.animate = animate as unknown as typeof Element.prototype.animate
    const { rerender } = render(<Num value={3} />)
    rerender(<Num value={9} />)
    expect(screen.getByText('9')).toBeInTheDocument()
    expect(animate).not.toHaveBeenCalled()
  })

  it('ticks once when the value changes, not on a re-render with the same value', () => {
    const animate = allowMotion()
    const { rerender } = render(<Num value={3} />)
    rerender(<Num value={3} />)
    expect(animate).not.toHaveBeenCalled()
    rerender(<Num value={4} />)
    expect(animate).toHaveBeenCalledTimes(1)
    expect(screen.getByText('4')).toBeInTheDocument()
  })
})
