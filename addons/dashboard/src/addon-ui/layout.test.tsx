import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { AddonNode } from './AddonNode'
import { parseNode } from './nodes'

const show = (node: unknown) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AddonNode node={node} addon="demo" />
    </QueryClientProvider>,
  )
const md = (text: string, extra: Record<string, unknown> = {}) => ({ type: 'markdown', text, ...extra })

describe('markdown headings get core-made ids (toc)', () => {
  const text = '# Title\n\n## Rules\n\ntext\n\n### Rules\n\n## Rules\n\n```\n## not a heading\n```\n\n## Loader query'
  it('ids are addon-h-<slug>, de-duplicated with -2 and -3, and "On this page" links to them', async () => {
    const { container } = show(md(text, { toc: true }))
    await screen.findByRole('heading', { name: 'Loader query' })
    const ids = [...container.querySelectorAll('.addon-md h1, .addon-md h2, .addon-md h3')].map((h) => h.id)
    expect(ids).toEqual(['addon-h-title', 'addon-h-rules', 'addon-h-rules-2', 'addon-h-rules-3', 'addon-h-loader-query'])
    const nav = await screen.findByRole('navigation', { name: 'On this page' })
    expect(within(nav).getAllByRole('button').map((b) => b.textContent)).toEqual(['Rules', 'Rules', 'Rules', 'Loader query'])
  })
  it('without toc no ids exist at all, and an id or anchor written in the markdown never survives', async () => {
    const { container } = show(md('## Rules\n\n<h2 id="evil">x</h2>\n\n<a id="evil2" name="n">y</a>', { toc: false }))
    await screen.findByRole('heading', { name: 'Rules' })
    expect(container.querySelectorAll('[id]')).toHaveLength(0)
    const on = show(md('## Rules\n\n## Other\n\n<h2 id="evil">x</h2>', { toc: true }))
    await waitFor(() => expect(on.container.querySelector('#addon-h-rules')).not.toBeNull())
    expect(on.container.querySelector('#evil')).toBeNull()
    expect(on.container.querySelector('[id^="user-content"]')).toBeNull()
  })
  it('fenced code is one block, not a chip per line', async () => {
    const { container } = show(md('```sql\nselect 1\nfrom t\n```'))
    await screen.findByText(/select 1/)
    expect(container.querySelectorAll('pre')).toHaveLength(1)
    expect(container.querySelector('.addon-md')!.className).toContain('[&_pre]:bg-surface-3')
    expect(container.querySelector('.addon-md')!.className).toContain('[&_pre_code]:bg-transparent')
    expect(container.querySelectorAll('pre code')).toHaveLength(1)
  })
})

describe('layout nodes', () => {
  it('an empty stack takes no space: no gap between its neighbours', () => {
    const { container } = show({ type: 'stack', children: [{ type: 'alert', tone: 'info', title: 'A' }, { type: 'stack', children: [] }, { type: 'alert', tone: 'info', title: 'B' }] })
    expect(container.firstElementChild!.children).toHaveLength(2)
  })
  it('fold: closed by default, its node is not rendered until opened', async () => {
    show({ type: 'fold', label: 'Recently merged', count: 2, node: { type: 'alert', tone: 'info', title: 'inside' } })
    const b = screen.getByRole('button', { name: /Recently merged\s*2/ })
    expect(b).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('inside')).not.toBeInTheDocument()
    await userEvent.click(b)
    expect(screen.getByText('inside')).toBeInTheDocument()
    expect(parseNode({ type: 'fold', label: 'x', node: null, extra: 1 }).ok).toBe(false)
  })
  it('popover: a button that opens a panel holding one node', async () => {
    show({ type: 'popover', label: 'Add', node: { type: 'alert', tone: 'info', title: 'panel body' } })
    expect(screen.queryByText('panel body')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByText('panel body')).toBeInTheDocument()
  })
  it('a row stack with fit keeps each child at its own width', () => {
    const { container } = show({ type: 'stack', direction: 'row', fit: true, children: [{ type: 'alert', tone: 'info', title: 'A' }] })
    expect(container.firstElementChild!.className).toMatch(/flex-wrap/)
    expect(container.firstElementChild!.className).not.toMatch(/flex-1/)
  })
  it('a pressed button is exposed as pressed', () => {
    show({ type: 'button', label: 'Filter', action: 'filter', args: { repo: 'x' }, pressed: true })
    expect(screen.getByRole('button', { name: 'Filter' })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('row actions and cells', () => {
  const table = (extra: Record<string, unknown>) => ({
    type: 'table',
    columns: [{ key: 'name', label: 'Name' }, { key: 'k', label: 'Ticket', cell: 'ticket' }],
    rows: [{ name: 'one', k: 'DEMO-0041' }],
    ...extra,
  })
  it('refuses unknown cell kinds and a rowOpen without an action', () => {
    expect(parseNode(table({ columns: [{ key: 'a', label: 'A', cell: 'html' }] })).ok).toBe(false)
    expect(parseNode(table({ rowOpen: {} })).ok).toBe(false)
    expect(parseNode(table({ rowOpen: { action: 'open', args: { slug: '$row.slug' } } })).ok).toBe(true)
  })
  it('primary on a row action decides the button; the rest go into More; without it the first non-danger is the button', async () => {
    const actions = (primary: boolean) => [
      { label: 'First', action: 'a', variant: 'ghost' },
      { label: 'Second', action: 'b', variant: 'secondary', ...(primary ? { primary: true } : {}) },
    ]
    const plain = (rowActions: unknown) => ({ type: 'table', columns: [{ key: 'name', label: 'Name' }], rows: [{ name: 'one' }], rowActions })
    const a = show(plain(actions(false)))
    expect(await screen.findByRole('button', { name: 'First' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Second' })).not.toBeInTheDocument()
    a.unmount()
    show(plain(actions(true)))
    expect(await screen.findByRole('button', { name: 'Second' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'First' })).not.toBeInTheDocument()
  })
  it('totalRow draws the last row as the total', () => {
    show({ type: 'table', totalRow: true, columns: [{ key: 'a', label: 'A' }], rows: [{ a: 'x' }, { a: 'Total' }] })
    expect(screen.getByText('Total').closest('tr')!.className).toMatch(/font-semibold/)
    expect(screen.getByText('x').closest('tr')!.className).not.toMatch(/font-semibold/)
  })
})

describe('charts', () => {
  const chart = { type: 'chart', kind: 'bar', layout: 'horizontal', unit: 'CHF', valueLabels: true, title: 'Cost by model', xKey: 'm', series: [{ key: 'v', label: 'Cost' }], points: [{ m: 'a', v: 2 }, { m: 'b', v: 1 }] }
  it('a title is the heading and the accessible name', async () => {
    show(chart)
    expect(await screen.findByRole('img', { name: 'Cost by model' })).toBeInTheDocument()
    expect(screen.getByText('Cost by model').tagName).toBe('FIGCAPTION')
  })
  it('refuses an unknown layout', () => {
    expect(parseNode({ ...chart, layout: 'diagonal' }).ok).toBe(false)
  })
})

describe('round 2 hardening', () => {
  it('args are capped: at most 16 keys, keys up to 64 characters', () => {
    const many = Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`k${i}`, 1]))
    expect(parseNode({ type: 'button', label: 'x', action: 'a', args: many }).ok).toBe(false)
    expect(parseNode({ type: 'button', label: 'x', action: 'a', args: { ['k'.repeat(65)]: 1 } }).ok).toBe(false)
    expect(parseNode({ type: 'button', label: 'x', action: 'a', args: { ok: 1 } }).ok).toBe(true)
    expect(parseNode({ type: 'table', columns: [{ key: 'a', label: 'A' }], rows: [], rowOpen: { action: 'o', args: many } }).ok).toBe(false)
    expect(parseNode({ type: 'list', items: [{ title: 't', actions: [{ label: 'l', action: 'a', args: many }] }] }).ok).toBe(false)
  })
  it('an id from addon text (a footnote) never survives, with or without toc', async () => {
    const text = 'Claim[^1]\n\n[^1]: the note\n\n## Rules\n\n## Other'
    const a = show(md(text))
    await screen.findByText(/the note/)
    expect(a.container.querySelectorAll('[id]')).toHaveLength(0)
    a.unmount()
    const b = show(md(text, { toc: true }))
    await screen.findByText(/the note/)
    const ids = [...b.container.querySelectorAll('[id]')]
    expect(ids.every((e) => /^H[1-6]$/.test(e.tagName) && e.id.startsWith('addon-h-'))).toBe(true) // only core's heading ids
    expect(ids.map((e) => e.id)).toEqual(expect.arrayContaining(['addon-h-rules', 'addon-h-other']))
  })
  it('two toc nodes on one page do not share heading ids', async () => {
    const both = show({ type: 'stack', children: [md('## Rules\n\n## Two', { toc: true }), md('## Rules\n\n## Two', { toc: true })] })
    await waitFor(() => expect(both.container.querySelectorAll('h2')).toHaveLength(4))
    await waitFor(() => {
      const ids = [...both.container.querySelectorAll('h2')].map((h) => h.id)
      expect(new Set(ids).size).toBe(4)
    })
  })
})
