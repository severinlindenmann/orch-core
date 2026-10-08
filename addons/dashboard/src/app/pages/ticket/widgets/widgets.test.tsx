import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { sha256Hex } from '@/api/sha256'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 5000 }
const widget = (id: string) => document.querySelector(`[data-widget="${id}"]`) as HTMLElement
const frames = () => [...document.querySelectorAll('iframe')] as HTMLIFrameElement[]

describe('widgets in the ticket Overview (DEMO-0043)', () => {
  it('draws the bars widget as an SVG with a title, inside the Context section, with its source and caption', async () => {
    renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    const w = await waitFor(() => {
      const el = widget('size')
      expect(el).toBeTruthy()
      return el
    }, T)
    expect(within(w).getByRole('heading', { level: 3, name: 'Bundle size, kB' })).toBeInTheDocument()
    const svg = w.querySelector('svg[role="img"]')!
    expect(svg.querySelector('title')).toHaveTextContent(/Bundle size, kB.*main 412.*branch 286/)
    expect(svg).toHaveAttribute('role', 'img')
    expect(w).toHaveTextContent('size.txt')
    expect(w).toHaveTextContent('core')
    expect(w.closest('section')).toHaveAttribute('aria-labelledby', 'sec-context')
    expect(w.querySelector('iframe')).toBeNull() // core types never use a frame
  })
  it('draws the checks table with a word and glyph per verdict, labelled as the agent claim', async () => {
    renderApp('/ticket/DEMO-0043')
    const w = await waitFor(() => {
      const el = widget('ac-status')
      expect(el).toBeTruthy()
      return el
    }, T)
    expect(within(w).getByText('AC1')).toBeInTheDocument()
    expect(within(w).getByText('Not met')).toBeInTheDocument()
    expect(within(w).getAllByText('Unproven')).toHaveLength(2)
    expect(w).toHaveTextContent(/the agent's check/i)
    expect(w.closest('section')).toHaveAttribute('aria-labelledby', 'sec-verification')
  })
  it('draws the template as a sandboxed frame in an addon frame for widgets', async () => {
    renderApp('/ticket/DEMO-0043')
    const w = await waitFor(() => {
      const el = widget('valid-from')
      expect(el).toBeTruthy()
      return el
    }, T)
    const frame = w.querySelector('iframe')!
    expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
    expect(frame.getAttribute('sandbox')).not.toMatch(/allow-same-origin|allow-top-navigation|allow-popups|allow-forms/)
    expect(frame.srcdoc).toContain("default-src 'none'")
    expect(frame.srcdoc).toContain('id="orch-data"')
    expect(frame.srcdoc).toContain('TIMESTAMP')
    expect(w.querySelector('[data-addon="widgets"]')).toBeTruthy()
    expect(w).toHaveTextContent(/agent HTML · before-after@1/)
    expect(w).toHaveTextContent(/sandboxed/i)
  })
  it('keeps agent HTML off when the widgets addon is disabled, but still draws core types', async () => {
    renderApp('/ticket/DEMO-0043', {
      setup: (s) => {
        const ws = s.workspaces.find((w) => w.prefix === 'DEMO')!.id
        s.addonOp(ws, 'widgets', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
      },
    })
    await waitFor(() => expect(widget('size')).toBeTruthy(), T)
    await waitFor(() => expect(widget('valid-from')).toBeTruthy(), T)
    expect(widget('size').querySelector('svg[role="img"]')).toBeTruthy()
    expect(widget('valid-from').querySelector('iframe')).toBeNull()
    expect(widget('valid-from')).toHaveTextContent(/agent HTML is off/i)
  })
})

describe('html widgets (DEMO-0041)', () => {
  it('runs a one-off page whose sha256 matches the artifact content, and shows a mismatch as code', async () => {
    renderApp('/ticket/DEMO-0041')
    await waitFor(() => expect(widget('proto')).toBeTruthy(), T)
    const ok = widget('proto')
    expect(ok.querySelector('iframe')).toHaveAttribute('sandbox', 'allow-scripts')
    expect(ok).toHaveTextContent(/agent HTML · one-off/)
    const art = mockStore.ticket('DEMO-0041')!.artifacts.find((a) => a.name === 'reconciliation-demo.html')!
    expect(ok.querySelector('iframe')!.srcdoc).toContain(art.preview!.slice(0, 40))
    expect(sha256Hex(art.preview!)).toBe(art.sha256)

    const bad = widget('proto-old')
    expect(bad).toBeTruthy()
    expect(bad.querySelector('iframe')).toBeNull()
    expect(bad).toHaveTextContent('sha256 does not match')
    expect(bad).toHaveTextContent('tolerance-demo.html') // the raw block is shown as code
    expect(frames()).toHaveLength(1)
  })
})

describe('mixed tickets', () => {
  it('DEMO-0047 (Findings): a long table scrolls in its own box; many bars; duplicate ids and invalid JSON are code with a reason', async () => {
    renderApp('/ticket/DEMO-0047')
    await waitFor(() => expect(widget('compare')).toBeTruthy(), T)
    const box = within(widget('compare')).getByRole('region', { name: /scrollable table/i })
    expect(box.className).toMatch(/overflow-auto/)
    expect(within(box).getAllByRole('row').length).toBeGreaterThanOrEqual(15)
    const bars = widget('runtime')
    expect(bars.querySelectorAll('svg rect[data-bar]').length).toBeGreaterThanOrEqual(12)
    expect(within(widget('verdict')).getByText('Dagster')).toBeInTheDocument()
    const dups = [...document.querySelectorAll('[data-widget-error]')].filter((e) => /id "notes" is used by more than one widget/.test(e.textContent ?? ''))
    expect(dups).toHaveLength(2)
    expect([...document.querySelectorAll('[data-widget-error]')].some((e) => /invalid JSON/.test(e.textContent ?? ''))).toBe(true)
    expect(document.querySelectorAll('h2').length).toBeGreaterThan(2) // the page structure is intact
  })
  it('DEMO-0046: unknown template, drift, unknown key and a refused place are code with their reason; others draw', async () => {
    renderApp('/ticket/DEMO-0046')
    await waitFor(() => expect(widget('incident')).toBeTruthy(), T)
    const errs = [...document.querySelectorAll('[data-widget-error]')].map((e) => e.textContent ?? '')
    expect(errs.some((t) => /unknown widget template/.test(t))).toBe(true)
    expect(errs.some((t) => /Drift/.test(t) && /pin/.test(t))).toBe(true)
    expect(errs.some((t) => /unknown key "colour"/.test(t))).toBe(true)
    expect(errs.some((t) => /widgets are not drawn in Requirements/.test(t))).toBe(true)
    expect(widget('incident').querySelector('dl')).toBeTruthy()
  })
  it('collapses widgets after the first two when a ticket has more than four, and expands on demand', async () => {
    const { user } = renderApp('/ticket/DEMO-0046')
    await waitFor(() => expect(widget('incident')).toBeTruthy(), T)
    const collapsed = [...document.querySelectorAll('[data-widget][data-open="false"]')] as HTMLElement[]
    expect(collapsed.length).toBeGreaterThan(0)
    expect(document.querySelectorAll('[data-widget][data-open="true"]').length).toBeGreaterThanOrEqual(2)
    const first = collapsed[0]
    expect(first.querySelector('iframe')).toBeNull()
    await user.click(within(first).getByRole('button', { name: /^Expand/ }))
    expect(first).toHaveAttribute('data-open', 'true')
  })
  it('Show text reveals the text alternative of a core widget', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await waitFor(() => expect(widget('size')).toBeTruthy(), T)
    await user.click(within(widget('size')).getByRole('button', { name: 'Show text' }))
    expect(within(widget('size')).getByText(/main: 412/)).toBeInTheDocument()
  })
})
