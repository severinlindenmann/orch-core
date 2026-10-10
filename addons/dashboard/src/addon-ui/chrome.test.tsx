/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { AddonFrame } from './AddonFrame'
import { AddonNode } from './AddonNode'
import { CollapsibleStack } from './AddonSlot'
import type { ResolvedContribution } from './slots'

const wrap = (ui: React.ReactNode) => render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>)

describe('AddonFrame header', () => {
  it('shows the A and the title only: no package id, no slot id', () => {
    render(
      <AddonFrame addon="publish" addonTitle="Publish" title="Shares" slot="today.card">
        body
      </AddonFrame>,
    )
    const header = screen.getByRole('banner', { hidden: true })
    expect(header).toHaveTextContent('Shares')
    expect(header.textContent).not.toMatch(/publish|today\.card/)
    expect(screen.getByRole('img', { name: 'From the Publish addon' })).toBeInTheDocument()
  })
  it('the title is an h3 by default and takes a level', () => {
    const { rerender } = render(<AddonFrame addon="publish" title="Shares" />)
    expect(screen.getByRole('heading', { level: 3, name: 'Shares' })).toBeInTheDocument()
    rerender(<AddonFrame addon="publish" title="Shares" level={2} />)
    expect(screen.getByRole('heading', { level: 2, name: 'Shares' })).toBeInTheDocument()
  })
})

describe('frame headers across the app', () => {
  it('Today and a ticket show no slot ids or package ids in addon frame headers', async () => {
    renderApp('/')
    await screen.findByRole('heading', { level: 1, name: /Today/ })
    // Today's addon cards are Glance items (an h3 per item, no frame header); frames elsewhere keep their header.
    const headers = '[data-addon] > header, [aria-labelledby="glance-h"] li h3'
    await waitFor(() => expect(document.querySelectorAll(headers).length).toBeGreaterThan(0))
    for (const h of document.querySelectorAll(headers)) expect(h.textContent).not.toMatch(/today\.card|ticket\.panel|\bnav\b|decision/)
  })
})

describe('addon page', () => {
  it('has exactly one A in its page header and no second framed header', async () => {
    renderApp('/addon/publish/shares')
    await screen.findByText(/Live shares/, {}, { timeout: 8000 })
    const main = document.querySelector('main') as HTMLElement
    expect(within(main).getAllByRole('img', { name: /^From / })).toHaveLength(1)
    expect(main.querySelector('[data-addon] > header')).toBeNull()
    const h1 = within(main).getByRole('heading', { level: 1 })
    expect(h1.querySelector('[role="img"]')).not.toBeNull()
  })
})

const item = (id: string): ResolvedContribution => ({ addon: 'publish', addonTitle: 'Publish', slot: 'ticket.panel', id, title: id[0].toUpperCase() + id.slice(1), node: { type: 'markdown', text: `body of ${id}` } })

describe('collapsible slot stack', () => {
  beforeEach(() => localStorage.clear())
  const stack = () => wrap(<CollapsibleStack items={['shares', 'logs', 'apps', 'notes'].map(item)} readOnly={false} />)
  it('starts collapsed with aria-expanded=false and a 32 px header button', () => {
    stack()
    const b = screen.getByRole('button', { name: /Shares/ })
    expect(b).toHaveAttribute('aria-expanded', 'false')
    expect(b.className).toMatch(/h-8/)
    expect(screen.queryByText('body of shares')).toBeNull()
  })
  it('keeps at most 2 open: opening a third closes the oldest', async () => {
    const user = userEvent.setup()
    stack()
    for (const n of ['Shares', 'Logs', 'Apps']) await user.click(screen.getByRole('button', { name: new RegExp(n) }))
    const open = screen.getAllByRole('button', { expanded: true })
    expect(open.map((b) => b.textContent)).toEqual([expect.stringContaining('Logs'), expect.stringContaining('Apps')])
    expect(screen.getByRole('button', { name: /Shares/ })).toHaveAttribute('aria-expanded', 'false')
  })
  it('opens a panel that appears while the page is open (the answer to an action), closing the oldest beyond 2', async () => {
    const user = userEvent.setup()
    const ids = ['shares', 'logs', 'apps']
    const view = wrap(<CollapsibleStack items={ids.map(item)} readOnly={false} />)
    for (const n of ['Shares', 'Logs']) await user.click(screen.getByRole('button', { name: new RegExp(n) }))
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <CollapsibleStack items={[...ids, 'session'].map(item)} readOnly={false} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /Session/ })).toHaveAttribute('aria-expanded', 'true'))
    expect(screen.getByRole('button', { name: /Shares/ })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: /Logs/ })).toHaveAttribute('aria-expanded', 'true')
  })
  it('the baseline is taken once states have loaded: loaded panels stay collapsed, one that later appears opens', async () => {
    const waiting = { status: 'pending' as const, retry: () => {} }
    const loading = ['shares', 'logs'].map((id) => ({ ...item(id), node: null, waiting }))
    const view = wrap(<CollapsibleStack items={loading} readOnly={false} />)
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <CollapsibleStack items={['shares'].map(item)} readOnly={false} />
      </QueryClientProvider>,
    )
    view.rerender(
      <QueryClientProvider client={new QueryClient()}>
        <CollapsibleStack items={['shares', 'logs'].map(item)} readOnly={false} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /Logs/ })).toHaveAttribute('aria-expanded', 'true'))
    expect(screen.getByRole('button', { name: /Shares/ })).toHaveAttribute('aria-expanded', 'false')
  })
  it('a panel that goes away and comes back keeps the earlier choice: it does not open as new', async () => {
    const user = userEvent.setup()
    const both = ['shares', 'logs']
    const at = (ids: string[]) => (
      <QueryClientProvider client={new QueryClient()}>
        <CollapsibleStack items={ids.map(item)} readOnly={false} />
      </QueryClientProvider>
    )
    const view = render(at(both))
    await user.click(screen.getByRole('button', { name: /Shares/ })) // open Shares, leave Logs shut
    view.rerender(at(['shares']))
    view.rerender(at(both))
    expect(screen.getByRole('button', { name: /Logs/ })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('button', { name: /Shares/ })).toHaveAttribute('aria-expanded', 'true')
  })
  it('another ticket starts from the remembered choices, not from what was open on the last one', async () => {
    const user = userEvent.setup()
    const ticket = (key: string) => ({ key }) as never
    const at = (key: string, ids: string[]) => (
      <QueryClientProvider client={new QueryClient()}>
        <CollapsibleStack items={ids.map(item)} ctx={{ ticket: ticket(key) }} readOnly={false} />
      </QueryClientProvider>
    )
    const view = render(at('DEMO-1', ['shares', 'logs']))
    await user.click(screen.getByRole('button', { name: /Logs/ }))
    localStorage.removeItem('orch.panel.publish/logs') // the remembered choice for the next ticket: shut
    view.rerender(at('DEMO-2', ['shares', 'logs']))
    await waitFor(() => expect(screen.getByRole('button', { name: /Logs/ })).toHaveAttribute('aria-expanded', 'false'))
  })
  it('remembers what the person opened', async () => {
    const user = userEvent.setup()
    const first = stack()
    await user.click(screen.getByRole('button', { name: /Notes/ }))
    expect(localStorage.getItem('orch.panel.publish/notes')).toBe('1')
    first.unmount()
    stack()
    expect(screen.getByRole('button', { name: /Notes/ })).toHaveAttribute('aria-expanded', 'true')
  })
})

describe('list and table nodes', () => {
  const actions = [
    { label: 'Open', action: 'open', variant: 'secondary' },
    { label: 'Copy link', action: 'copy', variant: 'ghost' },
    { label: 'Revoke', action: 'revoke', variant: 'danger' },
  ]
  it('a row with 3 actions shows one button and a "More actions for" menu, danger last', async () => {
    const user = userEvent.setup()
    wrap(<AddonNode node={{ type: 'list', items: [{ title: 'share/a', actions }] }} addon="publish" />)
    expect(screen.getByRole('button', { name: 'Open' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
    await user.click(screen.getByRole('button', { name: 'More actions for share/a' }))
    const items = screen.getAllByRole('menuitem')
    expect(items.map((i) => i.textContent)).toEqual(['Copy link', 'Revoke'])
    expect(items[1].className).toMatch(/danger|destructive/)
  })
  it('a table row names its menu after the first cell', () => {
    wrap(<AddonNode node={{ type: 'table', columns: [{ key: 'name', label: 'Name' }], rows: [{ name: 'wt-1' }], rowActions: actions }} addon="worktrees" />)
    expect(screen.getByRole('button', { name: 'More actions for wt-1' })).toBeInTheDocument()
  })
  it('the actions column header has a name', () => {
    wrap(<AddonNode node={{ type: 'table', columns: [{ key: 'name', label: 'Name' }], rows: [{ name: 'wt-1' }], rowActions: actions }} addon="worktrees" />)
    expect(screen.getAllByRole('columnheader').every((h) => (h.textContent ?? '').trim() !== '')).toBe(true)
  })
  it('status columns use the core status labels', () => {
    wrap(<AddonNode node={{ type: 'table', columns: [{ key: 'status', label: 'Status' }, { key: 'note', label: 'Note' }], rows: [{ status: 'in-progress', note: 'in-progress' }] }} addon="worktrees" />)
    expect(screen.getByText('In progress')).toBeInTheDocument()
    expect(screen.getByText('in-progress')).toBeInTheDocument() // other columns stay as the addon wrote them
  })
  it('a frame node\'s iframe is named by its title', () => {
    wrap(<AddonNode node={{ type: 'frame', title: 'Bars', html: '<p>hi</p>' }} addon="widgets" />)
    expect(document.querySelector('iframe')).toHaveAttribute('title', 'Bars')
  })
  it('a frame node inside a frame has a hairline and a Sandboxed chip, not a second addon frame', () => {
    wrap(<AddonNode node={{ type: 'frame', title: 'Bars', html: '<p>hi</p>' }} addon="widgets" />)
    expect(screen.getByText('Sandboxed')).toBeInTheDocument()
    expect(document.querySelector('[data-addon] header')).toBeNull()
    expect(screen.queryByRole('heading')).toBeNull()
  })
  it('prose markdown is capped at 72ch', async () => {
    wrap(<AddonNode node={{ type: 'markdown', text: 'hello' }} addon="wiki" />)
    expect((await screen.findByText('hello')).closest('[class*="72ch"]')).not.toBeNull()
  })
})

const tokens = readFileSync(resolve(process.cwd(), 'src/styles/tokens.css'), 'utf8') // vitest hands CSS imports back empty
// --- tokens (jsdom cannot compute colour, so the contrast is computed from the token values)
const tok = (name: string): string => {
  const m = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`).exec(tokens)
  if (!m) throw new Error(`token ${name}`)
  return m[1]
}
const lum = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
const ratio = (a: string, b: string) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
  return (x + 0.05) / (y + 0.05)
}
describe('tokens', () => {
  it.each(['bg', 'surface', 'surface-2', 'sidebar'])('--text-faint is at least 4.5:1 on --%s', (bg) => {
    expect(ratio(tok('text-faint'), tok(bg))).toBeGreaterThanOrEqual(4.5)
  })
  it('chart series never use the addon orange', () => {
    expect(tokens).not.toMatch(/--chart-\d:\s*var\(--addon\)/)
  })
})

describe('code nodes', () => {
  it('a shell command wraps (all of it visible in the 320 px rail); other code scrolls', () => {
    wrap(<AddonNode node={{ type: 'code', language: 'bash', text: 'orch session start --in background DEMO-0044' }} addon="start-agent" />)
    expect(document.querySelector('pre[data-language="bash"]')!.className).toMatch(/whitespace-pre-wrap/)
    wrap(<AddonNode node={{ type: 'code', language: 'json', text: '{}' }} addon="start-agent" />)
    expect(document.querySelector('pre[data-language="json"]')!.className).toMatch(/overflow-x-auto/)
  })
})
