import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { unshareable } from '@/api/shares'
import type { Artifact, TicketDocument } from '@/api/types'
import { Artifacts } from './Artifacts'
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
    expect(within(card).getByText('reconcile-pass.log').closest('a, button')).toBeNull()
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
  const tasks = (exit?: number) => [{ id: 'T1', receipt: exit === undefined ? null : { exit, ms: 5 } }]
  function cardOf(artifact: object, tasksState: unknown) {
    const ticket = { key: 'DEMO-1', artifacts: [artifact], people: {}, tasks_state: tasksState } as unknown as TicketDocument
    const viewer = { name: (p: string) => p, role: 'owner' } as unknown as Viewer
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <Artifacts ticket={ticket} viewer={viewer} jump={() => {}} sign={() => {}} />
      </QueryClientProvider>,
    )
    return screen.getByRole('list', { name: 'Artifacts' }).querySelector('li')!
  }
  it('a host receipt with exit 0 is titled "Check of T1" with a CHECK badge, the hash behind Details, no Publish', () => {
    const card = cardOf(art(), tasks(0))
    expect(within(card).getByText('Check of T1')).toBeInTheDocument()
    expect(within(card).getByTestId('type-badge')).toHaveTextContent('CHECK')
    expect(within(card).getByText('Details')).toBeInTheDocument()
    expect(within(card).queryByRole('button', { name: /Publish/ })).toBeNull()
  })
  it('checkOf: passed, failed, and no recorded status', () => {
    expect(checkOf(art(), tasks(0))).toBe('passed')
    expect(checkOf(art(), tasks(2))).toBe('failed')
    expect(checkOf(art(), tasks())).toBeNull()
    expect(checkOf(art(), undefined)).toBeNull()
  })
  it('a failing status says so on the badge', () => {
    const card = cardOf(art(), tasks(1))
    expect(within(card).getByText('Check of T1')).toBeInTheDocument()
    expect(within(card).getByTestId('type-badge')).toHaveTextContent('CHECK')
  })
  it('a file an agent named x-receipt.txt is a normal file, never a check', () => {
    const agent = art({ name: 'x-receipt.txt', kind: 'other', task: undefined, added_by: 'claude-code', added_by_kind: 'agent' })
    expect(checkOf(agent, tasks(0))).toBeNull()
    const card = cardOf(agent, tasks(0))
    expect(within(card).getByText('x-receipt.txt')).toBeInTheDocument()
    expect(within(card).getByTestId('type-badge')).not.toHaveTextContent('CHECK')
  })
  it('an agent-added file of kind receipt is not a check either, and a host receipt with no status claims nothing', () => {
    expect(checkOf(art({ added_by: 'claude-code', added_by_kind: 'agent' }), tasks(0))).toBeNull()
    expect(checkOf(art({ added_by: 'host', added_by_kind: 'agent' }), tasks(0))).toBeNull() // the name "host" is not the host
    expect(checkOf(art({ added_by: 'host', added_by_kind: undefined }), tasks(0))).toBeNull()
    const card = cardOf(art(), tasks())
    expect(within(card).getByText('T1-receipt.log')).toBeInTheDocument()
  })
  it('an empty file cannot be published as a link', () => {
    expect(unshareable({ name: 'a.txt', kind: 'other', bytes: 0 })).toBe('The file is empty.')
    expect(unshareable({ name: 'a.txt', kind: 'other', bytes: 5 })).toBeNull()
  })
})
