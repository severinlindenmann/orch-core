import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { AddonNode, GLANCE_ROWS } from './AddonNode'
import { parseNode } from './nodes'

const show = (node: unknown, glance = false) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AddonNode node={node} addon="demo" glance={glance ? {} : undefined} />
    </QueryClientProvider>,
  )

const table = (extra: Record<string, unknown> = {}) => ({
  type: 'table',
  columns: [
    { key: 'name', label: 'App' },
    { key: 'status', label: 'State', cell: 'state' },
  ],
  rows: [
    { id: 'a', name: 'Alpha', status: 'running' },
    { id: 'b', name: 'Beta', status: 'stopped' },
    { id: 'c', name: 'Gamma', status: 'failed' },
  ],
  rowDetail: {
    key: 'id',
    nodes: {
      a: { type: 'kv', pairs: [{ label: 'CPU', value: '12 %' }] },
      b: { type: 'kv', pairs: [{ label: 'CPU', value: '–' }] },
      c: { type: 'script', src: 'x' },
    },
  },
  ...extra,
})

beforeEach(() => mockStore.setViewer('p_sev'))

describe('table rowDetail', () => {
  it('is strict: unknown keys and bad keys are refused', () => {
    expect(parseNode(table()).ok).toBe(true)
    expect(parseNode(table({ rowDetail: { key: 'id', nodes: {}, html: '<b>' } })).ok).toBe(false)
    expect(parseNode(table({ rowDetail: { key: 'a.b', nodes: {} } })).ok).toBe(false)
  })
  it('draws a chevron on rows with a detail and opens one row at a time', async () => {
    const user = userEvent.setup()
    show(table({ rows: [...table().rows, { id: 'd', name: 'Delta', status: 'running' }] }))
    expect(screen.queryByRole('button', { name: 'Details for Delta' })).toBeNull()
    const alpha = screen.getByRole('button', { name: 'Details for Alpha' })
    const beta = screen.getByRole('button', { name: 'Details for Beta' })
    expect(alpha).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('12 %')).toBeNull()
    await user.click(alpha)
    expect(alpha).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById(alpha.getAttribute('aria-controls')!)).toHaveTextContent('12 %')
    await user.click(beta)
    expect(alpha).toHaveAttribute('aria-expanded', 'false')
    expect(beta).toHaveAttribute('aria-expanded', 'true')
    expect(screen.queryByText('12 %')).toBeNull()
    await user.click(beta)
    expect(beta).toHaveAttribute('aria-expanded', 'false')
  })
  it('an invalid detail node becomes the "could not be shown" box, only in its own row', async () => {
    const user = userEvent.setup()
    show(table())
    await user.click(screen.getByRole('button', { name: 'Details for Gamma' }))
    expect(screen.getByRole('alert')).toHaveTextContent('This addon panel could not be shown.')
    expect(screen.getByText('Alpha')).toBeInTheDocument()
  })
})

describe('link copy', () => {
  it('shows the http(s) address with a Copy button and opens it with noopener', () => {
    show({ type: 'link', label: 'App address', href: 'https://alpha.apps.acme.example', copy: true })
    const a = screen.getByRole('link', { name: /App address: https:\/\/alpha\.apps\.acme\.example/ })
    expect(a).toHaveAttribute('target', '_blank')
    expect(a.getAttribute('rel')).toMatch(/noopener/)
    expect(a.getAttribute('rel')).toMatch(/noreferrer/)
    expect(screen.getByRole('button', { name: 'Copy App address' })).toHaveTextContent('Copy')
  })
  it('still refuses anything but http(s)', () => {
    expect(parseNode({ type: 'link', label: 'x', href: 'javascript:alert(1)', copy: true }).ok).toBe(false)
  })
})

describe('glance presentation', () => {
  it('a stat is one line with its label, the hint below and a sparkline for its trend, without a box', () => {
    const { container } = show({ type: 'stat', label: 'last 7 days', value: 'CHF 31.40', hint: 'CHF 39.91 this month', trend: [3, 5, 4, 6] }, true)
    expect(screen.getByText('CHF 31.40').parentElement).toHaveTextContent('CHF 31.40 last 7 days')
    expect(screen.getByText('CHF 39.91 this month')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Trend over 4 values, from 3 to 6/ })).toBeInTheDocument()
    expect(container.querySelector('.rounded-md.border')).toBeNull()
  })
  it(`a list shows ${GLANCE_ROWS} rows, then "n more"`, () => {
    const items = Array.from({ length: 5 }, (_, i) => ({ title: `#${i} PR`, subtitle: 'repo', badge: 'checks pass' }))
    show({ type: 'list', items }, true)
    const list = screen.getByRole('list')
    expect(within(list).getAllByRole('listitem')).toHaveLength(GLANCE_ROWS)
    expect(screen.getByText('2 more')).toBeInTheDocument()
  })
  it('the trend is validated: at most 60 finite numbers', () => {
    expect(parseNode({ type: 'stat', label: 'x', value: 1, trend: Array(61).fill(1) }).ok).toBe(false)
    expect(parseNode({ type: 'stat', label: 'x', value: 1, trend: ['1'] }).ok).toBe(false)
  })
})

describe('review fixes', () => {
  it.each([
    ['a right-to-left override', 'https://good.example/‮gpj.exe'],
    ['an isolate', 'https://good.example/⁦x⁩'],
    ['a zero-width space', 'https://good​.example'],
    ['a control character', 'https://good.example/\u0007'],
    ['a space', 'https://good.example/a b'],
  ])('a link with %s is refused', (_, href) => {
    expect(parseNode({ type: 'link', label: 'x', href, copy: true }).ok).toBe(false)
    expect(parseNode({ type: 'link', label: 'x', href }).ok).toBe(false)
  })
  it('a long address is shown in full, wrapped and isolated, never cut off', () => {
    const href = `https://billing.apps.acme.example.attacker.example/${'a'.repeat(300)}`
    show({ type: 'link', label: 'App address', href, copy: true })
    const a = screen.getByRole('link', { name: /App address/ })
    expect(a).toHaveTextContent(href)
    const bdi = a.querySelector('bdi')!
    expect(bdi.textContent).toBe(href)
    expect(bdi.className).toMatch(/break-all/)
    expect(a.querySelector('.truncate')).toBeNull()
  })
  it('rowDetail nodes that are missing (a $ref to no state) drop the chevrons, not the table', () => {
    const n = table({ rowDetail: { key: 'id', nodes: null } })
    expect(parseNode(n).ok).toBe(true)
    show(n)
    expect(screen.getByText('Alpha')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Details for/ })).toBeNull()
    expect(parseNode(table({ rowDetail: { key: 'id', nodes: Object.fromEntries(Array.from({ length: 501 }, (_, i) => [`k${i}`, null])) } })).ok).toBe(true)
  })
  it('two rows that share a detail key still open one at a time', async () => {
    const user = userEvent.setup()
    show(table({ rows: [{ id: 'a', name: 'Alpha', status: 'running' }, { id: 'a', name: 'Alpha copy', status: 'running' }] }))
    const one = screen.getByRole('button', { name: 'Details for Alpha' })
    const two = screen.getByRole('button', { name: 'Details for Alpha copy' })
    await user.click(one)
    expect(one).toHaveAttribute('aria-expanded', 'true')
    expect(two).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getAllByText('12 %')).toHaveLength(1)
  })
  it('a sparkline of extreme finite values has no NaN coordinates; the label rounds', () => {
    show({ type: 'stat', label: 'x', value: 1, trend: [-1e12, 1e12, 0.1 + 0.2] }, true)
    const img = screen.getByRole('img', { name: 'Trend over 3 values, from -1,000,000,000,000 to 0.3' })
    expect(img.innerHTML).not.toMatch(/NaN|Infinity/)
    expect(parseNode({ type: 'stat', label: 'x', value: 1, trend: [1e13] }).ok).toBe(false)
  })
})
