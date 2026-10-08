import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Fragment } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { AddonPackage, Workspace } from '@/api/types'
import workspacesFixture from '@/mocks/fixtures/workspaces.json'
import addonsFixture from '@/mocks/fixtures/addons.json'
import { WorkspaceProvider } from '@/app/workspace'
import { AddonNode } from './AddonNode'
import { resolveBindings } from './bindings'
import { selectContributions, type SlotContext } from './slots'

function renderNode(node: unknown, { addon, ctx, withWorkspace }: { addon: string; ctx?: SlotContext; withWorkspace?: boolean }) {
  const Wrap = withWorkspace ? WorkspaceProvider : Fragment
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Wrap>
        <AddonNode node={node} addon={addon} ctx={ctx} />
      </Wrap>
    </QueryClientProvider>,
  )
}

/** The id of the first workspace (WorkspaceProvider's default). */
const WS = workspacesFixture[0].id
/** Action buttons stay disabled until the workspace is known; click once enabled. */
async function clickWhenEnabled(name: string) {
  const b = screen.getByRole('button', { name })
  await waitFor(() => expect(b).toBeEnabled())
  await userEvent.click(b)
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

  it('renders markdown with gfm', async () => {
    show({ type: 'markdown', text: '# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n[docs](https://example.com)' })
    expect(await screen.findByRole('heading', { name: 'Title' })).toBeInTheDocument()
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
  it('strips <script> and javascript: links, and never loads images', async () => {
    const { container } = show({
      type: 'markdown',
      text: 'hello <script>window.__pwned = 1</script>\n\n[bad](javascript:alert(1))\n\n![tracker](https://example.com/t.png)\n\n<img src="https://example.com/x.png" onerror="alert(1)">',
    })
    await screen.findByText('bad')
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
  const addons = addonsFixture as unknown as AddonPackage[]
  const workspace = workspacesFixture[0] as unknown as Workspace

  it('selects nothing without a workspace (deny by default)', () => {
    expect(selectContributions(addons, 'nav')).toEqual([])
  })

  it("returns github's board.lane with the issues of its state", () => {
    const issueItems = [{ title: 'a' }, { title: 'b' }, { title: 'c' }]
    const lanes = selectContributions(addons, 'board.lane', { workspace, addon: { issueItems } })
    expect(lanes).toHaveLength(1)
    expect(lanes[0].addon).toBe('github')
    expect(lanes[0].title).toBe('External · GitHub issues')
    expect((lanes[0].node as { items: unknown[] }).items).toHaveLength(3)
  })

  it('skips addons inactive in the workspace and contributions whose `when` binding is empty', () => {
    const off = { ...workspace, addons: { ...workspace.addons, github: { ...workspace.addons.github, enabled: false } } }
    expect(selectContributions(addons, 'board.lane', { workspace: off })).toHaveLength(0)
    const ungranted = { ...workspace, addons: { ...workspace.addons, github: { ...workspace.addons.github, status: 'needs_grant' as const } } }
    expect(selectContributions(addons, 'board.lane', { workspace: ungranted })).toHaveLength(0)
    const withPr = selectContributions(addons, 'ticket.panel', { workspace, ticket: { key: 'T' } as never, addon: { prByTicket: { T: { number: 3 } } } })
    expect(withPr.some((c) => c.addon === 'github')).toBe(true)
    const without = selectContributions(addons, 'ticket.panel', { workspace, ticket: { key: 'T' } as never, addon: { prByTicket: {} } })
    expect(without.some((c) => c.addon === 'github')).toBe(false)
  })
})

describe('new node types', () => {
  it('renders list item actions and posts the args', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    renderNode({ type: 'list', items: [{ title: 'share/a', actions: [{ label: 'Revoke', action: 'revoke', args: { id: 'a' }, variant: 'danger' }] }] }, { addon: 'publish', withWorkspace: true })
    await clickWhenEnabled('Revoke')
    expect(post).toHaveBeenCalledWith(WS, 'publish', 'revoke', expect.objectContaining({ id: 'a' }))
  })
  it('resolves $row.<key> in table row action args', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    renderNode(
      { type: 'table', columns: [{ key: 'id', label: 'Id' }], rows: [{ id: 'r7' }], rowActions: [{ label: 'Drop', action: 'drop', args: { id: '$row.id', fixed: 'x' } }] },
      { addon: 'publish', withWorkspace: true },
    )
    await clickWhenEnabled('Drop')
    expect(post).toHaveBeenCalledWith(WS, 'publish', 'drop', expect.objectContaining({ id: 'r7', fixed: 'x' }))
  })
  const evilList = { type: 'list', items: [{ title: 'x', actions: [{ label: 'Go', action: 'go', args: { ticket: 'X-1', ws: 'evil', keep: 'k' } }] }] }
  it('drops ws/ticket from addon args when core has no such context', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    post.mockClear()
    renderNode(evilList, { addon: 'publish', withWorkspace: true })
    await clickWhenEnabled('Go')
    expect(post.mock.calls[0][0]).toBe(WS)
    const body = post.mock.calls[0][3]
    expect(body).toEqual({ keep: 'k' })
    expect(body).not.toHaveProperty('ticket')
    expect(body).not.toHaveProperty('ws')
  })
  it('lets only core set ticket when a ticket context exists', async () => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    post.mockClear()
    renderNode(evilList, { addon: 'publish', withWorkspace: true, ctx: { ticket: { key: 'DEMO-1' } as never } })
    await clickWhenEnabled('Go')
    expect(post.mock.calls[0][3]).toEqual({ keep: 'k', ticket: 'DEMO-1' })
  })
  it.each(['javascript:alert(1)', 'http://example.com/x', 'data:text/html,hi'])('does not open an action result url of %s', async (url) => {
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done', url })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    renderNode({ type: 'button', label: 'Go', action: 'go' }, { addon: 'publish', withWorkspace: true })
    await clickWhenEnabled('Go')
    await waitFor(() => expect(post).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 50)) // let onSuccess run
    expect(open).not.toHaveBeenCalled()
    open.mockRestore()
  })
  it('opens an https action result url', async () => {
    vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done', url: 'https://github.com/x/y' })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    renderNode({ type: 'button', label: 'Go', action: 'go' }, { addon: 'publish', withWorkspace: true })
    await clickWhenEnabled('Go')
    await waitFor(() => expect(open).toHaveBeenCalledWith('https://github.com/x/y', '_blank', 'noopener,noreferrer'))
    open.mockRestore()
  })
  it('renders a frame sandboxed without same-origin', () => {
    renderNode({ type: 'frame', title: 'Bars', html: '<p>hi</p>' }, { addon: 'widgets' })
    const f = screen.getByTitle('Bars') as HTMLIFrameElement
    expect(f.getAttribute('sandbox')).toBe('allow-scripts')
    expect(f.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(f.srcdoc).toContain("default-src 'none'")
    expect(f.srcdoc.indexOf('Content-Security-Policy')).toBeLessThan(f.srcdoc.indexOf('<p>hi</p>'))
  })
  it('replaces a frame that navigates away (a second load) with the fallback box', () => {
    renderNode({ type: 'frame', title: 'Bars', html: '<p>hi</p>' }, { addon: 'widgets' })
    fireEvent.load(screen.getByTitle('Bars'))
    expect(screen.getByTitle('Bars')).toBeInTheDocument() // the first load is the srcdoc itself
    fireEvent.load(screen.getByTitle('Bars'))
    expect(screen.queryByTitle('Bars')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent(/could not be shown/i)
  })
  it('refuses a terminal node from an addon without a pty grant', () => {
    renderNode({ type: 'terminal', session: 't1' }, { addon: 'wiki' })
    expect(screen.getByText(/could not be shown/i)).toBeInTheDocument()
  })
  it('a terminal node from an addon with pty naming a session that does not exist is not shown', async () => {
    renderNode({ type: 'terminal', session: 't1' }, { addon: 'terminals', withWorkspace: true })
    expect(await screen.findByText(/could not be shown/i)).toBeInTheDocument()
  })
  it('renders alert and progress', () => {
    renderNode({ type: 'stack', children: [{ type: 'alert', tone: 'warn', title: 'Budget at 80%' }, { type: 'progress', label: 'Children', value: 7, max: 25 }] }, { addon: 'usage' })
    expect(screen.getByRole('status')).toHaveTextContent('Budget at 80%')
    expect(screen.getByRole('progressbar', { name: 'Children' })).toHaveAttribute('aria-valuenow', '7')
  })
})
