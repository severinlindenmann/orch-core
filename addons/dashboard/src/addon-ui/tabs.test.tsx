import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import { AddonNode } from './AddonNode'
import { MAX_DEPTH, parseNode } from './nodes'

const md = (text: string) => ({ type: 'markdown', text })
const tabs = (extra: Record<string, unknown> = {}) => ({
  type: 'tabs',
  id: 'main',
  tabs: [
    { id: 'one', label: 'First', count: 3, node: md('first panel') },
    { id: 'two', label: 'Second', node: md('second panel') },
  ],
  ...extra,
})
const show = (node: unknown) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AddonNode node={node} addon="demo" />
    </QueryClientProvider>,
  )
const KEY = (person: string) => `orch.addon-tab..${person}.demo.main`
/** Tabs appear once the viewer ("me") has loaded: who is looking decides which tab is remembered. */
async function showTabs(node: unknown) {
  const r = show(node)
  await screen.findAllByRole('tab')
  return r
}

beforeEach(() => {
  localStorage.clear()
  mockStore.setViewer('p_sev')
})
afterEach(() => vi.restoreAllMocks())

describe('tabs node schema', () => {
  it('accepts tabs with an optional count and an untyped nested node', () => {
    expect(parseNode(tabs()).ok).toBe(true)
    expect(parseNode({ ...tabs(), tabs: [{ id: 'a', label: 'A', count: null, node: null }] }).ok).toBe(true)
  })
  it.each([
    ['an unknown key on the node', tabs({ extra: 1 })],
    ['an unknown key on a tab', { ...tabs(), tabs: [{ id: 'a', label: 'A', node: md('x'), icon: 'x' }] }],
    ['no tabs', tabs({ tabs: [] })],
    ['duplicate tab ids', { ...tabs(), tabs: [{ id: 'a', label: 'A', node: md('x') }, { id: 'a', label: 'B', node: md('y') }] }],
    ['a bad tab id', { ...tabs(), tabs: [{ id: '../x', label: 'A', node: md('x') }] }],
    ['a negative count', { ...tabs(), tabs: [{ id: 'a', label: 'A', count: -1, node: md('x') }] }],
    ['more than 12 tabs', { ...tabs(), tabs: Array.from({ length: 13 }, (_, i) => ({ id: `t${i}`, label: 'T', node: md('x') })) }],
  ])('refuses %s', (_name, node) => {
    expect(parseNode(node).ok).toBe(false)
  })
})

describe('tabs node render', () => {
  it('shows the first tab, its count, and renders only the open panel', async () => {
    await showTabs(tabs())
    expect(screen.getByRole('tab', { name: /First\s*3/ })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('first panel')).toBeInTheDocument()
    expect(screen.queryByText('second panel')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Second' }))
    expect(await screen.findByText('second panel')).toBeInTheDocument()
    expect(screen.queryByText('first panel')).not.toBeInTheDocument()
  })
  it('remembers the tab per addon and node id, and comes back to it', async () => {
    const a = await showTabs(tabs())
    await userEvent.click(screen.getByRole('tab', { name: 'Second' }))
    a.unmount()
    await showTabs(tabs())
    expect(screen.getByRole('tab', { name: 'Second' })).toHaveAttribute('aria-selected', 'true')
    expect(localStorage.getItem(KEY('p_sev'))).toBe('two')
  })
  it('two viewers on one browser keep their own choice', async () => {
    const a = await showTabs(tabs())
    await userEvent.click(screen.getByRole('tab', { name: 'Second' }))
    await waitFor(() => expect(localStorage.getItem(KEY('p_sev'))).toBe('two'))
    a.unmount()
    mockStore.setViewer('p_mara')
    await showTabs(tabs())
    expect(await screen.findByRole('tab', { name: /First/ })).toHaveAttribute('aria-selected', 'true')
    await userEvent.click(screen.getByRole('tab', { name: 'Second' }))
    await waitFor(() => expect(localStorage.getItem(KEY('p_mara'))).toBe('two'))
    expect(localStorage.getItem(KEY('p_sev'))).toBe('two')
  })
  it('another node id (or addon) does not share the choice', async () => {
    const a = await showTabs(tabs())
    await userEvent.click(screen.getByRole('tab', { name: 'Second' }))
    a.unmount()
    await showTabs(tabs({ id: 'other' }))
    expect(screen.getByRole('tab', { name: /First/ })).toHaveAttribute('aria-selected', 'true')
  })
  it('falls back to the first tab when the remembered one is gone, and works without storage', async () => {
    localStorage.setItem(KEY('p_sev'), 'removed')
    const a = await showTabs(tabs())
    expect(screen.getByRole('tab', { name: /First/ })).toHaveAttribute('aria-selected', 'true')
    a.unmount()
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    await showTabs(tabs())
    await userEvent.click(screen.getByRole('tab', { name: 'Second' }))
    expect(await screen.findByText('second panel')).toBeInTheDocument()
  })
  it('validates a nested node when it is rendered: one bad panel does not take the tabs down', async () => {
    await showTabs({ type: 'tabs', id: 'bad', tabs: [{ id: 'a', label: 'A', node: md('fine') }, { id: 'b', label: 'B', node: { type: 'script', src: 'x' } }] })
    expect(await screen.findByText('fine')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'B' }))
    expect(screen.getByText('This addon panel could not be shown.')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'A' })).toBeInTheDocument()
  })
  it('counts toward the depth limit like any other nesting', async () => {
    let node: unknown = md('deep')
    for (let i = 0; i <= MAX_DEPTH; i++) node = { type: 'tabs', id: `t${i}`, tabs: [{ id: 'a', label: 'A', node }] }
    show(node)
    expect(await screen.findByText('This addon panel could not be shown.')).toBeInTheDocument()
  })
})

describe('table presentation', () => {
  const table = {
    type: 'table',
    columns: [{ key: 'name', label: 'Name' }, { key: 'n', label: 'Count' }, { key: 'cost', label: 'Cost', align: 'right' }, { key: 'status', label: 'State', cell: 'state' }, { key: 'state', label: 'Plain' }],
    rows: [{ name: 'a', n: 3, cost: 'CHF 1.00', status: 'running', state: 'idle' }, { name: 'b', n: null, cost: 'CHF 2.00', status: 'failed', state: 'idle' }],
  }
  it('right-aligns columns of numbers and columns marked align right, with tabular figures', () => {
    show(table)
    expect(screen.getByRole('columnheader', { name: 'Count' })).toHaveClass('text-right')
    expect(screen.getByRole('columnheader', { name: 'Cost' })).toHaveClass('text-right')
    expect(screen.getByRole('columnheader', { name: 'Name' })).not.toHaveClass('text-right')
    expect(screen.getByText('3').closest('td')).toHaveClass('text-right', 'tabular-nums')
    expect(screen.getByText('CHF 1.00').closest('td')).toHaveClass('text-right')
  })
  it('shows a state word as a chip with a dot', () => {
    show(table)
    expect(screen.getByText('running')).toHaveClass('rounded-full')
    expect(screen.getByText('failed').firstElementChild).toHaveClass('bg-danger')
  })
  it('only a column marked cell state draws chips; the column name alone does not', () => {
    show(table)
    expect(screen.getAllByText('idle')[0]).not.toHaveClass('rounded-full')
  })
  it('refuses an unknown align', () => {
    expect(parseNode({ ...table, columns: [{ key: 'a', label: 'A', align: 'center' }] }).ok).toBe(false)
  })
})
