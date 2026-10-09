import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { blockText, CATALOG, fenced, PROPOSED_NOTE } from '@/api/widgetCatalog'
import { AddonNode } from './AddonNode'
import { parseNode } from './nodes'

// The `widget` node draws one ticket widget through core's strict parser. Templates get a frame only on the widgets
// addon's own surfaces while that addon is active; any other addon's widget node draws core types only.
const ws = vi.hoisted(() => ({ active: true }))
vi.mock('@/app/workspace', () => ({
  useWorkspace: () => ({ workspace: { addons: { widgets: { enabled: ws.active, status: 'active' } } }, workspaces: [] }),
}))

const ex = (ref: string) => blockText(CATALOG.find((c) => c.ref === ref)!.example)
const figure = (id: string) => waitFor(() => {
  const el = document.querySelector(`figure[data-widget="${id}"]`)
  expect(el).toBeTruthy()
  return el as HTMLElement
})

beforeEach(() => {
  ws.active = true
})

describe('widget node schema', () => {
  it('is in the closed node set, strict, with a 64 KiB cap on the block', () => {
    expect(parseNode({ type: 'widget', block: '{}' })).toMatchObject({ ok: true, node: { source: false } })
    expect(parseNode({ type: 'widget', block: 'x'.repeat(64 * 1024 + 1) }).ok).toBe(false)
    expect(parseNode({ type: 'widget', block: '{}', html: '<b>' }).ok).toBe(false) // unknown keys are refused, not stripped
    expect(parseNode({ type: 'widget-index', groups: [{ label: 'Core', items: [{ label: 'stats', widget: 'ex-stats' }] }] }).ok).toBe(true)
    expect(parseNode({ type: 'widget-index', groups: [{ label: 'Core', items: [{ label: 'x', widget: '"]*' }] }] }).ok).toBe(false)
    expect(parseNode({ type: 'widget-index', groups: [{ label: 'Core', items: [], href: 'x' }] }).ok).toBe(false)
  })
})

describe('widget node template gate', () => {
  it('the widgets addon, active: a template draws in the sandboxed frame', async () => {
    render(<AddonNode addon="widgets" node={{ type: 'widget', block: ex('flow@1') }} />)
    const f = await figure('ex-flow')
    expect(f.querySelector('iframe')).toHaveAttribute('sandbox', 'allow-scripts')
  })
  it('another addon, even with widgets active: no frame, the "off" card instead', async () => {
    render(<AddonNode addon="other" node={{ type: 'widget', block: ex('flow@1') }} />)
    const f = await figure('ex-flow')
    expect(f.querySelector('iframe')).toBeNull()
    expect(within(f).getByText(/Agent HTML is off/)).toBeInTheDocument()
  })
  it('the widgets addon, inactive: no frame', async () => {
    ws.active = false
    render(<AddonNode addon="widgets" node={{ type: 'widget', block: ex('image-compare@1') }} />)
    expect((await figure('ex-image-compare')).querySelector('iframe')).toBeNull()
  })
  it('a core type draws for any addon; a broken block is refused with plain words, never thrown', async () => {
    render(<AddonNode addon="other" node={{ type: 'stack', children: [{ type: 'widget', block: ex('stats') }, { type: 'widget', block: '{"type":"callout","role":"info","text":"x","__proto__":{"y":1}}' }] }} />)
    expect((await figure('ex-stats')).querySelector('iframe')).toBeNull()
    await waitFor(() => expect(document.querySelector('[data-state="refused"]')).toBeTruthy())
    expect(screen.getByRole('alert')).toHaveTextContent('This widget uses a setting orch does not know ("__proto__")')
  })
})

// Every catalog entry, drawn as the gallery draws it (a widget node with its source), one test each.
describe.each(CATALOG.map((c) => [c.ref, c] as const))('gallery entry %s', (_ref, c) => {
  it('draws, is not refused, frames only templates, and copies its fenced source', async () => {
    const user = userEvent.setup()
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<AddonNode addon="widgets" node={{ type: 'widget', block: blockText(c.example), source: true }} />)
    const f = await figure(String(c.example.id))
    expect(document.querySelector('[data-state="refused"]')).toBeNull()
    if (c.kind === 'template') expect(f.querySelector('iframe')).toHaveAttribute('sandbox', 'allow-scripts')
    else expect(f.querySelector('iframe')).toBeNull()
    const src = document.querySelector('[data-widget-source]') as HTMLElement
    await user.click(within(src).getByRole('button', { name: 'Copy source' }))
    expect(writeText).toHaveBeenCalledWith(fenced(c.example))
    expect(await within(src).findByText('Copied')).toBeInTheDocument()
    if (c.proposed) expect(PROPOSED_NOTE).toMatch(/not in orch\.widgets\.v1/)
  })
})

describe('source folding below 1280 px', () => {
  it('"Show source" toggles the source body; at xl it is always shown by CSS', async () => {
    const user = userEvent.setup()
    render(<AddonNode addon="widgets" node={{ type: 'widget', block: ex('stats'), source: true }} />)
    await figure('ex-stats')
    const body = document.querySelector('[data-source-body]') as HTMLElement
    expect(body.className).toMatch(/(^|\s)hidden(\s|$)/)
    expect(body.className).toMatch(/xl:block/)
    await user.click(screen.getByRole('button', { name: 'Show source' }))
    expect(body.className).not.toMatch(/(^|\s)hidden(\s|$)/)
    expect(screen.getByRole('button', { name: 'Hide source' })).toHaveAttribute('aria-expanded', 'true')
  })
})
