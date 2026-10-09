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
    expect(w).not.toHaveTextContent(/sandboxed/i) // a core widget is not an addon contribution
    expect(w).not.toHaveAttribute('data-addon')
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
    expect(w).toHaveAttribute('data-addon', 'widgets')
    expect(frame).toHaveAttribute('title', 'Sandboxed preview · before-after@1')
    expect(within(w).getByRole('heading', { level: 3 })).toHaveTextContent(/· Sandboxed/)
    expect(w.querySelectorAll('[data-addon]')).toHaveLength(0) // one card, no nested addon box
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
    const { user } = renderApp('/ticket/DEMO-0041')
    await waitFor(() => expect(widget('proto')).toBeTruthy(), T)
    const ok = widget('proto')
    expect(ok.querySelector('iframe')).toHaveAttribute('sandbox', 'allow-scripts')
    expect(ok.querySelector('iframe')).toHaveAttribute('title', 'Sandboxed preview · one-off')
    const art = mockStore.ticket('DEMO-0041')!.artifacts.find((a) => a.name === 'reconciliation-demo.html')!
    expect(ok.querySelector('iframe')!.srcdoc).toContain(art.preview!.slice(0, 40))
    expect(sha256Hex(art.preview!)).toBe(art.sha256)

    const bad = widget('proto-old')
    expect(bad).toBeTruthy()
    expect(bad.querySelector('iframe')).toBeNull()
    expect(within(bad).getByRole('alert')).toHaveTextContent(/changed after it was pinned/)
    expect(bad).not.toHaveTextContent('sha256 does not match') // the technical text is behind Details
    expect(within(bad).queryByRole('button', { name: /retry/i })).toBeNull()
    expect(bad).not.toHaveTextContent('"html"') // the raw block is behind Details > Show block
    await user.click(within(bad).getByRole('button', { name: 'Details' }))
    expect(bad).toHaveTextContent('sha256 does not match')
    await user.click(within(bad).getByRole('button', { name: 'Show block' }))
    expect(bad).toHaveTextContent('tolerance-demo.html')
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
    const errs = [...document.querySelectorAll('[data-widget-error]')].map((e) => e.textContent ?? '')
    expect(errs.filter((t) => /Two widgets use the id "notes"\. Ask its author to rename one\./.test(t))).toHaveLength(2)
    expect(errs.some((t) => /could not be read/.test(t))).toBe(true)
    expect(document.querySelectorAll('h2').length).toBeGreaterThan(2) // the page structure is intact
  })
  it('DEMO-0046: unknown template, drift, unknown key and a refused place are code with their reason; others draw', async () => {
    renderApp('/ticket/DEMO-0046')
    await waitFor(() => expect(widget('incident')).toBeTruthy(), T)
    const errs = [...document.querySelectorAll('[data-widget-error]')].map((e) => e.textContent ?? '')
    expect(errs.some((t) => /uses a template orch does not know/.test(t))).toBe(true)
    expect(errs.some((t) => t.includes('This preview changed after it was pinned, so it is not shown. Ask the agent that wrote it to update the pin.'))).toBe(true)
    expect(errs.some((t) => t.includes('This widget uses a setting orch does not know ("colour"). Ask its author to fix the block.'))).toBe(true)
    expect(errs.some((t) => /Widgets are not shown in Requirements, because approvals sign that text\./.test(t))).toBe(true)
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    expect(widget('incident').querySelector('dl')).toBeTruthy()
  })
  it('has no outer collapsible: every drawn widget shows its body, and Expand opens a larger view', async () => {
    const { user } = renderApp('/ticket/DEMO-0046')
    await waitFor(() => expect(widget('incident')).toBeTruthy(), T)
    expect(document.querySelectorAll('[data-widget][data-open]').length).toBe(0)
    const body = widget('incident').querySelector('[data-widget-body]') as HTMLElement
    expect(body.className).toMatch(/max-h-\[280px\]|h-\[280px\]/)
    await user.click(within(widget('incident')).getByRole('button', { name: /^Expand/ }))
    const sheet = await screen.findByRole('dialog')
    expect(sheet).toHaveTextContent('Drawn by core.')
  })
  it('Show text reveals the text alternative of a core widget', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await waitFor(() => expect(widget('size')).toBeTruthy(), T)
    await user.click(within(widget('size')).getByRole('button', { name: 'Show text' }))
    expect(within(widget('size')).getByText(/main: 412/)).toBeInTheDocument()
  })
  it('shows a drift or mismatch refusal even when agent HTML is off; a valid frame gets the "off" card', async () => {
    renderApp('/ticket/DEMO-0041', {
      setup: (s) => {
        const ws = s.workspaces.find((w) => w.prefix === 'DEMO')!.id
        s.addonOp(ws, 'widgets', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
      },
    })
    await waitFor(() => expect(widget('proto-old')).toBeTruthy(), T)
    expect(widget('proto-old')).toHaveTextContent(/changed after it was pinned/)
    expect(widget('proto')).toHaveTextContent(/agent HTML is off/i)
    expect(frames()).toHaveLength(0)
  })
})

describe('catalog examples in the demo tickets', () => {
  it('DEMO-0043 shows stats and a series next to its checks, all drawn by core', async () => {
    renderApp('/ticket/DEMO-0043')
    await waitFor(() => expect(widget('seed-load')).toBeTruthy(), T)
    expect(within(widget('seed-load')).getByText('+9 since 09:00')).toBeInTheDocument()
    expect(within(widget('seed-load')).getByText('Warning')).toBeInTheDocument()
    expect(widget('seed-load').closest('section')).toHaveAttribute('aria-labelledby', 'sec-context')
    expect(widget('seed-time').querySelectorAll('[data-series]')).toHaveLength(2)
    expect(widget('seed-time').closest('section')).toHaveAttribute('aria-labelledby', 'sec-verification')
    expect(widget('ac-status')).toBeTruthy()
    for (const id of ['seed-load', 'seed-time']) expect(widget(id).querySelector('iframe')).toBeNull()
  })
  it('DEMO-0045 shows a timeline and a diff in Current state', async () => {
    renderApp('/ticket/DEMO-0045')
    await waitFor(() => expect(widget('so-far')).toBeTruthy(), T)
    expect(within(widget('so-far')).getByText('Failed')).toBeInTheDocument()
    expect(widget('mask-fix').querySelectorAll('[data-line="add"]')).toHaveLength(2)
    expect(widget('mask-fix').closest('section')).toHaveAttribute('aria-labelledby', 'sec-current_state')
  })
  it('DEMO-0046 shows a flow and an image compare in the sandbox, and a callout drawn by core', async () => {
    renderApp('/ticket/DEMO-0046')
    await waitFor(() => expect(widget('dup-flow')).toBeTruthy(), T)
    for (const id of ['dup-flow', 'report-card']) {
      const f = widget(id).querySelector('iframe')!
      expect(f, id).toHaveAttribute('sandbox', 'allow-scripts')
      expect(widget(id)).toHaveAttribute('data-addon', 'widgets')
    }
    expect(widget('report-card').querySelector('iframe')!.srcdoc).toContain('data:image/png;base64,')
    expect(widget('wait-rule').querySelector('[data-callout="warn"]')).toHaveTextContent(/^Warning: Do not merge/)
    expect(widget('wait-rule')).not.toHaveAttribute('data-addon')
  })
})
