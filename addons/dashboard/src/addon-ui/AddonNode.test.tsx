import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { AddonManifest } from '@/api/types'
import addonsFixture from '@/mocks/fixtures/addons.json'
import { AddonNode } from './AddonNode'
import { resolveBindings } from './bindings'
import { selectContributions, type SlotContext } from './slots'

function renderNode(node: unknown, { addon, ctx }: { addon: string; ctx?: SlotContext }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AddonNode node={node} addon={addon} ctx={ctx} />
    </QueryClientProvider>,
  )
}

const show = (node: unknown) => render(<AddonNode node={node} addon="demo" />)

describe('AddonNode', () => {
  it('renders a stat', () => {
    show({ type: 'stat', label: 'Live shares', value: 2, hint: 'this week' })
    expect(screen.getByText('Live shares')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('this week')).toBeInTheDocument()
  })

  it('renders a table', () => {
    show({
      type: 'table',
      columns: [
        { key: 'a', label: 'Ticket' },
        { key: 'b', label: 'PR' },
      ],
      rows: [{ a: 'DEMO-0041', b: '#29' }],
    })
    const table = screen.getByRole('table')
    expect(within(table).getByText('Ticket')).toBeInTheDocument()
    expect(within(table).getByText('DEMO-0041')).toBeInTheDocument()
    expect(within(table).getByText('#29')).toBeInTheDocument()
  })

  it('renders markdown with gfm', () => {
    show({ type: 'markdown', text: '# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n[docs](https://example.com)' })
    expect(screen.getByRole('heading', { name: 'Title' })).toBeInTheDocument()
    expect(screen.getByRole('table')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'docs' })).toHaveAttribute('href', 'https://example.com')
  })

  it('rejects an unknown node type with the fallback box', () => {
    show({ type: 'iframe', src: 'https://evil.example' })
    expect(screen.getByRole('alert')).toHaveTextContent('could not be shown')
    expect(screen.getByRole('img', { name: 'From addon: demo' })).toBeInTheDocument()
  })

  it('rejects an invalid node (bad props) and keeps valid siblings', () => {
    show({ type: 'stack', children: [{ type: 'stat', label: 'Ok', value: 1 }, { type: 'stat', value: 'no label' }, { type: 'link', label: 'x', href: 'javascript:alert(1)' }] })
    expect(screen.getByText('Ok')).toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(2)
  })
})

describe('sanitized markdown', () => {
  it('strips <script> and javascript: links, and never loads images', () => {
    const { container } = show({
      type: 'markdown',
      text: 'hello <script>window.__pwned = 1</script>\n\n[bad](javascript:alert(1))\n\n![tracker](https://example.com/t.png)\n\n<img src="https://example.com/x.png" onerror="alert(1)">',
    })
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.innerHTML).not.toContain('javascript:')
    expect(container.innerHTML).not.toContain('onerror')
    expect(screen.queryByRole('link', { name: 'bad' })).toBeNull()
    expect(screen.getByText('bad')).toBeInTheDocument()
  })
})

describe('bindings', () => {
  it('resolves $ref and interpolation against the context', () => {
    const ctx = { ticket: { key: 'DEMO-1', addons: { usage: { cents: 412 } } } }
    expect(resolveBindings({ $ref: 'ticket.key' }, ctx)).toBe('DEMO-1')
    expect(resolveBindings('CHF ${ticket.addons.usage.cents|cents}', ctx)).toBe('CHF 4.12')
    expect(resolveBindings({ $ref: 'ticket.nope' }, ctx)).toBeNull()
  })
})

describe('SlotRegistry', () => {
  const addons = addonsFixture as unknown as AddonManifest[]

  it("returns github's board.lane with its three issues", () => {
    const lanes = selectContributions(addons, 'board.lane')
    expect(lanes).toHaveLength(1)
    expect(lanes[0].addon).toBe('github')
    expect(lanes[0].title).toBe('External · GitHub issues')
    expect((lanes[0].node as { items: unknown[] }).items).toHaveLength(3)
  })

  it('skips disabled addons and contributions whose `when` binding is empty', () => {
    const off = addons.map((a) => (a.name === 'github' ? { ...a, enabled: false } : a))
    expect(selectContributions(off, 'board.lane')).toHaveLength(0)
    const withPr = selectContributions(addons, 'ticket.panel', { ticket: { key: 'T', addons: { github: { pr: { number: 3 } } } } as never })
    expect(withPr.some((c) => c.addon === 'github')).toBe(true)
    const without = selectContributions(addons, 'ticket.panel', { ticket: { key: 'T', addons: {} } as never })
    expect(without.some((c) => c.addon === 'github')).toBe(false)
  })
})

describe('new node types', () => {
  it('renders list item actions and posts the args', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    renderNode({ type: 'list', items: [{ title: 'share/a', actions: [{ label: 'Revoke', action: 'revoke', args: { id: 'a' }, variant: 'danger' }] }] }, { addon: 'publish' })
    await userEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(post).toHaveBeenCalledWith('publish', 'revoke', expect.objectContaining({ id: 'a' }))
  })
  it('resolves $row.<key> in table row action args', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    renderNode(
      { type: 'table', columns: [{ key: 'id', label: 'Id' }], rows: [{ id: 'r7' }], rowActions: [{ label: 'Drop', action: 'drop', args: { id: '$row.id', fixed: 'x' } }] },
      { addon: 'publish' },
    )
    await userEvent.click(screen.getByRole('button', { name: 'Drop' }))
    expect(post).toHaveBeenCalledWith('publish', 'drop', expect.objectContaining({ id: 'r7', fixed: 'x' }))
  })
  const evilList = { type: 'list', items: [{ title: 'x', actions: [{ label: 'Go', action: 'go', args: { ticket: 'X-1', ws: 'evil', keep: 'k' } }] }] }
  it('drops ws/ticket from addon args when core has no such context', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    post.mockClear()
    renderNode(evilList, { addon: 'publish' })
    await userEvent.click(screen.getByRole('button', { name: 'Go' }))
    const body = post.mock.calls[0][2]
    expect(body).toEqual({ keep: 'k' })
    expect(body).not.toHaveProperty('ticket')
    expect(body).not.toHaveProperty('ws')
  })
  it('lets only core set ticket when a ticket context exists', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    post.mockClear()
    renderNode(evilList, { addon: 'publish', ctx: { ticket: { key: 'DEMO-1' } as never } })
    await userEvent.click(screen.getByRole('button', { name: 'Go' }))
    expect(post.mock.calls[0][2]).toEqual({ keep: 'k', ticket: 'DEMO-1' })
  })
  it('renders a frame sandboxed without same-origin', () => {
    renderNode({ type: 'frame', title: 'Bars', html: '<p>hi</p>' }, { addon: 'widgets' })
    const f = screen.getByTitle('Bars') as HTMLIFrameElement
    expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    expect(f.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(f.srcdoc).toContain("default-src 'none'")
    expect(f.srcdoc.indexOf('Content-Security-Policy')).toBeLessThan(f.srcdoc.indexOf('<p>hi</p>'))
  })
  it('refuses a terminal node from an addon without a pty grant', () => {
    renderNode({ type: 'terminal', session: 't1' }, { addon: 'wiki' })
    expect(screen.getByText(/could not be shown/i)).toBeInTheDocument()
  })
  it('shows the placeholder alert for a terminal node from an addon with pty', async () => {
    renderNode({ type: 'terminal', session: 't1' }, { addon: 'terminals' })
    expect(await screen.findByText(/Terminal sessions arrive with the terminals addon/)).toBeInTheDocument()
  })
  it('renders alert and progress', () => {
    renderNode({ type: 'stack', children: [{ type: 'alert', tone: 'warn', title: 'Budget at 80%' }, { type: 'progress', label: 'Children', value: 7, max: 25 }] }, { addon: 'usage' })
    expect(screen.getByRole('status')).toHaveTextContent('Budget at 80%')
    expect(screen.getByRole('progressbar', { name: 'Children' })).toHaveAttribute('aria-valuenow', '7')
  })
})
