import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { render, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { workdirApi } from '@/api/workdir'
import { queries } from '@/api/queries'
import { unshareable } from '@/api/shares'
import type { Artifact, TicketDocument } from '@/api/types'
import { Artifacts, MissingDelivery, missingDelivery } from './Artifacts'
import { checkOf } from './EvidenceThumb'
import type { Viewer } from './shared'

const T = { timeout: 4000 }

afterEach(() => vi.restoreAllMocks())

async function artifactsTab(key = 'DEMO-0041') {
  const r = renderApp(`/ticket/${key}`)
  await screen.findByRole('heading', { level: 1 }, T)
  await r.user.click(screen.getByRole('tab', { name: /Results/ }))
  await screen.findByRole('list', { name: 'Artifacts' }, T)
  return r
}

// Owner request 2026-10-10 (G3): the ticket's artifact cards follow the Artifacts page model.
describe('Ticket artifacts: cards', () => {
  it('a card has one Preview button; its name is plain text; AC and task are links that look like links', async () => {
    const { user } = await artifactsTab()
    const preview = screen.getByRole('button', { name: 'Preview reconcile-pass.log' })
    const card = preview.closest('li')!
    expect(within(card).getAllByText('reconcile-pass.log')[0].closest('a, button')).toBeNull()
    await user.click(within(card).getByText('Details'))
    const ac = within(card).getByRole('button', { name: 'Proves AC1' })
    expect(ac.className).toMatch(/underline/)
    await user.click(preview)
    expect(await screen.findByRole('dialog', {}, T)).toBeInTheDocument()
  })
  it('no generated placeholder art on the cards, only the type', async () => {
    await artifactsTab()
    const card = screen.getByRole('button', { name: 'Preview reconcile-pass.log' }).closest('li')!
    expect(within(card).queryByRole('img', { name: /Generated placeholder/ })).toBeNull()
    expect(card.querySelector('svg[viewBox="0 0 240 150"]')).toBeNull()
  })
})

describe('Ticket artifacts: viewer', () => {
  it('Copy all says when it could not copy', async () => {
    const { user } = await artifactsTab()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    await user.click(screen.getByRole('button', { name: 'Preview reconcile-pass.log' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    await user.click(within(sheet).getByRole('button', { name: 'Copy all' }))
    expect(await within(sheet).findByText(/Could not copy/, {}, T)).toBeInTheDocument()
  })
  it('Copy all without a clipboard says so too', async () => {
    const { user } = await artifactsTab()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
    await user.click(screen.getByRole('button', { name: 'Preview reconcile-pass.log' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    await user.click(within(sheet).getByRole('button', { name: 'Copy all' }))
    expect(await within(sheet).findByText(/Could not copy/, {}, T)).toBeInTheDocument()
  })
  it('View source switches to "Show preview" and back', async () => {
    const { user } = await artifactsTab()
    await user.click(screen.getByRole('button', { name: 'Preview reconciliation-demo.html' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    await waitFor(() => expect(sheet.querySelector('iframe')).toHaveAttribute('sandbox', ''), T) // agent HTML is inert (security review #1)
    await user.click(within(sheet).getByRole('button', { name: 'View source' }))
    expect(sheet.querySelector('iframe')).toBeNull()
    await user.click(within(sheet).getByRole('button', { name: 'Show preview' }))
    await waitFor(() => expect(sheet.querySelector('iframe')).toHaveAttribute('sandbox', ''), T) // agent HTML is inert (security review #1)
  })
  it('read-only CSV rows do not light up on hover', async () => {
    const { user } = await artifactsTab('DEMO-0043')
    await user.click(screen.getByRole('button', { name: 'Preview tariff-source-diff.csv' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    const rows = within(sheet).getAllByRole('row')
    for (const r of rows) expect(r.className).toMatch(/hover:bg-transparent/)
  })
})

describe('run 15c P10: a check result card', () => {
  const sha = 'sha256:' + 'e3b0c442'.repeat(8)
  const art = (over: Record<string, unknown> = {}): Artifact => ({ name: 'T1-receipt.log', kind: 'receipt', task: 'T1', bytes: 0, sha256: sha, added_by: 'host', added_by_kind: 'host', at: '2026-10-30T10:00:00Z', ...over }) as Artifact
  const tasks = (exit?: number, attested: 'host' | 'agent' = 'host') => [{ id: 'T1', receipt: exit === undefined ? null : { exit, ms: 5, attested_by: attested } }]
  function cardOf(artifact: object | object[], tasksState: unknown) {
    const ticket = { key: 'DEMO-1', artifacts: Array.isArray(artifact) ? artifact : [artifact], acceptance_state: [], people: {}, tasks_state: tasksState } as unknown as TicketDocument
    const viewer = { name: (p: string) => p, role: 'owner' } as unknown as Viewer
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(queries.workspaces().queryKey, [])
    render(
      <QueryClientProvider client={client}>
        <Artifacts ticket={ticket} viewer={viewer} jump={() => {}} sign={() => {}} />
      </QueryClientProvider>,
    )
    return screen.getByRole('list', { name: 'Artifacts' }).querySelector('li')!
  }
  it('a host receipt is inside the missing-file card Details, with its verified result and no Publish', () => {
    const card = cardOf(art(), tasks(0))
    expect(within(card).getByText('Check passed').closest('details')).not.toBeNull()
    expect(card.firstElementChild).toHaveTextContent('No file was delivered yet.')
    expect(within(card).getByText("The agent's check").closest('details')).not.toBeNull()
    expect(within(card).queryByTestId('type-badge')).toBeNull()
    expect(within(card).getByText('Details')).toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: /Publish/ })).toBeNull()
  })
  it('names the requested file from tasks or acceptance and safely falls back', () => {
    for (const source of ['tasks_state', 'acceptance_state']) {
      expect(missingDelivery({ [source]: [{ text: 'Create `hello.txt`.' }] } as unknown as TicketDocument)).toBe('hello.txt was not delivered')
    }
    expect(missingDelivery({ tasks_state: [{ text: 'Check the greeting' }] } as unknown as TicketDocument)).toBe('No file was delivered yet.')
  })
  it('a delivered file leads even when a receipt came first; check preview is plain and facts are folded', () => {
    const card = cardOf([art({ preview: 'hello checked', bytes: 13 }), art({ name: 'hello.txt', kind: 'other', preview: 'hello', bytes: 5 })], tasks(0))
    expect(card.firstElementChild).toHaveTextContent('hello.txt')
    expect(within(card).getByRole('button', { name: 'Preview hello.txt' })).toBeVisible()
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
    fireEvent.click(within(card).getByText('Details'))
    fireEvent.click(within(card).getByRole('button', { name: "Preview The agent's check" }))
    const panel = screen.getByRole('dialog')
    expect(within(panel).getByRole('heading')).toHaveTextContent("The agent's check")
    expect(panel).toHaveTextContent('hello checked')
    expect(panel).not.toHaveTextContent(/T1|receipt|LOG|Publish as link/)
    expect(within(panel).getByText(/sha256/).closest('details')).not.toHaveAttribute('open')
  })
  it('receipt-only Results names the missing file and task logs stay in Details too', () => {
    const card = cardOf(art({ kind: 'log', bytes: 16 }), [{ ...tasks(0)[0], text: 'Add hello.txt' }])
    expect(card.firstElementChild).toHaveTextContent('hello.txt was not delivered')
    expect(within(card).getByText("The agent's check").closest('details')).not.toHaveAttribute('open')
    expect(card).not.toHaveTextContent(/T1|receipt|LOG|Publish as link|passed/)
  })
  it('checkOf: passed, failed, and no recorded status', () => {
    expect(checkOf(art(), tasks(0))).toBe('passed')
    expect(checkOf(art(), tasks(2))).toBe('failed')
    expect(checkOf(art(), tasks())).toBeNull()
    expect(checkOf(art(), undefined)).toBeNull()
    expect(checkOf(art(), tasks(0, 'agent'))).toBeNull() // an agent-attested receipt is a claim, not a passed check
  })
  it('an agent-attested receipt with exit 0 shows no passed claim or internal name', () => {
    const card = cardOf(art(), tasks(0, 'agent'))
    expect(card).not.toHaveTextContent(/T1|receipt|Check passed|LOG/)
    expect(within(card).queryByTestId('type-badge')).toBeNull()
  })
  it('a failing host status says so inside Details', () => {
    const card = cardOf(art(), tasks(1))
    expect(within(card).getByText('Check failed').closest('details')).not.toBeNull()
    expect(within(card).getByText("The agent's check").closest('details')).not.toBeNull()
    expect(within(card).queryByTestId('type-badge')).toBeNull()
  })
  it('a file an agent named x-receipt.txt is a normal file, never a check', () => {
    const agent = art({ name: 'x-receipt.txt', kind: 'other', task: undefined, added_by: 'claude-code', added_by_kind: 'agent' })
    expect(checkOf(agent, tasks(0))).toBeNull()
    const card = cardOf(agent, tasks(0))
    expect(within(card).getAllByText('x-receipt.txt')[0]).toBeInTheDocument()
    expect(within(card).queryByTestId('type-badge')).toBeNull()
  })
  it('an agent-added file of kind receipt is not a check either, and a host receipt with no status claims nothing', () => {
    expect(checkOf(art({ added_by: 'claude-code', added_by_kind: 'agent' }), tasks(0))).toBeNull()
    expect(checkOf(art({ added_by: 'host', added_by_kind: 'agent' }), tasks(0))).toBeNull() // the name "host" is not the host
    expect(checkOf(art({ added_by: 'host', added_by_kind: undefined }), tasks(0))).toBeNull()
    const card = cardOf(art(), tasks())
    expect(card).not.toHaveTextContent(/T1|receipt|Check passed|LOG/)
  })
  it('an empty file cannot be published as a link', () => {
    expect(unshareable({ name: 'a.txt', kind: 'other', bytes: 0 })).toBe('The file is empty.')
    expect(unshareable({ name: 'a.txt', kind: 'other', bytes: 5 })).toBeNull()
  })
})


describe('P11 working files', () => {
  function message(named = true, repos: string[] = [], artifacts: Artifact[] = []) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    client.setQueryData<unknown>([...queries.workspaces().queryKey], [{ id: 'demo', prefix: 'DEMO' }])
    const ticket = { key: 'DEMO-1', links: { repos }, artifacts, tasks_state: [{ text: named ? 'Create hello.txt' : 'Create a greeting' }] } as unknown as TicketDocument
    render(<QueryClientProvider client={client}><MissingDelivery ticket={ticket} /></QueryClientProvider>)
    return client
  }
  it.each([true, false])('lists a working file honestly, with plain size and no preview (named: %s)', async (named) => {
    vi.spyOn(workdirApi, 'get').mockResolvedValue({ available: true, exists: true, codebase: false, items: [{ name: 'hello.txt', bytes: 12 }, ...Array.from({ length: 6 }, (_, i) => ({ name: `other${i}.txt`, bytes: 1024 }))] })
    message(named)
    expect(await screen.findByText("hello.txt is in the ticket's work folder, but the agent did not add it as a result.")).toBeInTheDocument()
    const list = screen.getByRole('list', { name: "Files in the ticket's work folder" })
    expect(list).toHaveTextContent('hello.txt · 12 bytes')
    expect(list).toHaveTextContent('other0.txt · 1.0 KB')
    expect(within(list).getAllByRole('listitem')).toHaveLength(5)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByText(/was not delivered/)).toBeNull()
  })
  it.each([false, true])('keeps the missing-file message for an empty or absent folder (exists: %s)', async (exists) => {
    vi.spyOn(workdirApi, 'get').mockResolvedValue({ available: true, exists, codebase: false, items: [] })
    message()
    expect(await screen.findByText('hello.txt was not delivered')).toBeInTheDocument()
  })
  it('ignores query errors quietly', async () => {
    vi.spyOn(workdirApi, 'get').mockRejectedValue(new Error('offline'))
    const client = message(false)
    expect(await screen.findByText('No file was delivered yet.')).toBeInTheDocument()
    expect(client.getQueryState(['workdir', 'demo', 'DEMO-1'])?.status).toBe('error')
    expect(screen.queryByText(/offline/)).toBeNull()
  })
  it('does not query for a ticket with a codebase', () => {
    const get = vi.spyOn(workdirApi, 'get')
    message(true, ['repo'])
    expect(screen.getByText('hello.txt was not delivered')).toBeInTheDocument()
    expect(get).not.toHaveBeenCalled()
  })
  it('does not mistake another working file for the named result', async () => {
    vi.spyOn(workdirApi, 'get').mockResolvedValue({ available: true, exists: true, codebase: false, items: [{ name: 'other.txt', bytes: 12 }] })
    message()
    expect(await screen.findByText('hello.txt was not delivered')).toBeInTheDocument()
    expect(screen.queryByRole('list')).toBeNull()
  })
  it('does not query when a result was already delivered', () => {
    const get = vi.spyOn(workdirApi, 'get')
    message(true, [], [{ name: 'hello.txt', kind: 'other' } as Artifact])
    expect(get).not.toHaveBeenCalled()
  })
  it('shows the mock workdir fixture on DEMO-0045 Results', async () => {
    await artifactsTab('DEMO-0045')
    expect(await screen.findByText("hello.txt is in the ticket's work folder, but the agent did not add it as a result.", {}, T)).toBeInTheDocument()
    expect(screen.getByRole('list', { name: "Files in the ticket's work folder" })).toHaveTextContent('hello.txt · 12 bytes')
  })
})

describe('Ticket Results tab count (P11)', () => {
  it('counts delivered results only, not the agent\'s check logs', async () => {
    renderApp('/ticket/DEMO-0045')
    await screen.findByRole('heading', { level: 1 }, T)
    // DEMO-0045 has only task check logs: nothing delivered, so the tab says 0
    expect(screen.getByRole('tab', { name: /Results/ })).toHaveTextContent(/Results\s*0$/)
  })
})
