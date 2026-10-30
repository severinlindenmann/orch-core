import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { buildEdit, canEditTicket, EditTicket, TicketEditProvider } from './EditTicket'
import { AcceptanceTasks } from './AcceptanceTasks'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { handoffMode } from '@/api/features'
import type { TicketDocument } from '@/api/types'
import type { Viewer } from './shared'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: vi.fn(() => true) }))
// The signing dialog is its own feature (SignPopover tests); here it only shows the typed action it was given.
vi.mock('@/components/sign/SignPopover', () => ({
  SignPopover: (p: { title: string; action: unknown; inline?: boolean; lead?: React.ReactNode; confirmLabel?: string; onSettled?: (phase: string) => void; onClose: () => void }) => (
    <div role={p.inline ? 'region' : 'dialog'} aria-label={`sign ${p.title}`} data-confirm={p.confirmLabel}>
      {p.lead}
      <pre data-testid="sent-action">{JSON.stringify(p.action)}</pre>
      <button onClick={() => p.onSettled?.('done')}>signed</button>
      <button onClick={p.onClose}>cancelled</button>
    </div>
  ),
}))

// The first test pays the cold load of the lazy ticket page; on a busy machine that alone can pass 5 s.
vi.setConfig({ testTimeout: 30000 })
const T = { timeout: 15000 }
afterEach(() => { vi.restoreAllMocks(); vi.mocked(handoffMode).mockReturnValue(true) })

const ticket = {
  key: 'DEMO-0001',
  type: 'feature' as const,
  body: { summary: 'Old summary', requirements: 'Must work' },
  acceptance_state: [{ id: 'AC1', text: 'It opens', state: 'unproven' as const, evidence: [] }],
  people: { owner: 'p_a', assignees: ['p_b'], reviewers: [], watchers: [] },
  head: { seq: 7, hash: '' },
}
const none = { sections: {}, criteria: {}, added: [], people: { owner: 'p_a', assignees: ['p_b'], reviewers: [] } }

describe('buildEdit: what the person changed, as one typed action', () => {
  it('says nothing changed when nothing did', () => {
    expect(buildEdit(ticket, none, true)).toEqual({ nothing: true })
    expect(buildEdit(ticket, { ...none, sections: { summary: ' Old summary ' } }, true)).toEqual({ nothing: true })
  })
  it('carries only the changed sections, criteria and people, with the head it showed', () => {
    const made = buildEdit(
      ticket,
      {
        sections: { summary: 'New summary', requirements: 'Must work' },
        criteria: { AC1: 'It opens fast' },
        added: ['It saves', ' '],
        people: { owner: 'p_c', assignees: ['p_d'], reviewers: ['p_a'] },
      },
      true,
    )
    expect(made).toEqual({
      steps: 6,
      action: {
        action: 'edit_sequence',
        key: 'DEMO-0001',
        head_seq: 7,
        sections: { summary: 'New summary' },
        acceptance: { edit: { AC1: 'It opens fast' }, add: ['It saves'] },
        people: { owner: { add: ['p_c'], remove: [] }, assignees: { add: ['p_d'], remove: ['p_b'] }, reviewers: { add: ['p_a'], remove: [] } },
      },
    })
  })
  it('leaves the people out when the viewer may not change them', () => {
    expect(buildEdit(ticket, { ...none, people: { owner: 'p_c', assignees: [], reviewers: [] } }, false)).toEqual({ nothing: true })
  })
  it('keeps a spike\'s findings under the host\'s name', () => {
    const spike = { ...ticket, type: 'spike' as const, body: { verification: 'Found it' } }
    expect(buildEdit(spike, { ...none, sections: { verification: 'Found more' } }, true)).toMatchObject({ action: { sections: { findings: 'Found more' } } })
  })
  it('refuses to empty a section or a criterion, and too many changes', () => {
    expect(buildEdit(ticket, { ...none, sections: { summary: '  ' } }, true)).toHaveProperty('problem')
    expect(buildEdit(ticket, { ...none, criteria: { AC1: ' ' } }, true)).toHaveProperty('problem')
    expect(buildEdit(ticket, { ...none, added: Array.from({ length: 9 }, (_, i) => `c${i}`) }, true)).toHaveProperty('problem')
  })
})

describe('buildEdit: the title (r7-P07)', () => {
  const titled = { ...ticket, title: 'Invoice export' }
  it('is nothing when the title is as it was, one extra step when it changed', () => {
    expect(buildEdit(titled, { ...none, title: ' Invoice export ' }, true)).toEqual({ nothing: true })
    expect(buildEdit(titled, { ...none, title: 'Invoice export for Ben' }, true)).toEqual({
      steps: 1,
      action: { action: 'edit_sequence', key: 'DEMO-0001', head_seq: 7, title: 'Invoice export for Ben' },
    })
    expect(buildEdit(titled, { ...none, title: 'New', sections: { summary: 'New summary' } }, true)).toMatchObject({ steps: 2, action: { title: 'New' } })
  })
  it('refuses an empty, multi-line or overlong title in words', () => {
    expect(buildEdit(titled, { ...none, title: '  ' }, true)).toHaveProperty('problem')
    expect(buildEdit(titled, { ...none, title: 'a\nb' }, true)).toHaveProperty('problem')
    expect(buildEdit(titled, { ...none, title: 'x'.repeat(201) }, true)).toHaveProperty('problem')
  })
})

describe('the ticket page edit', () => {
  it('renaming from the heading is one short step: new title, Rename, the one review (r10-P09)', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await user.click(await screen.findByRole('button', { name: /^Change the title: Load tariff tables/ }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Rename DEMO-0043' }, T)
    expect(within(dialog).queryByRole('button', { name: /Review and sign/ })).toBeNull() // not the long form
    const field = within(dialog).getByLabelText('Title')
    await waitFor(() => expect(field).toHaveFocus(), T)
    const input = field as HTMLInputElement
    expect([input.selectionStart, input.selectionEnd]).toEqual([input.value.length, input.value.length]) // cursor at the end, nothing selected
    await user.type(field, ' v2{Enter}') // Enter goes through the same review as the button
    const sent = JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
    expect(sent).toMatchObject({ action: 'edit_sequence', key: 'DEMO-0043' })
    expect(sent.title).toMatch(/ v2$/)
    // r12-P11: no sheet on top of the box: the review is inline in it, shows old -> new, and Rename confirms
    expect(screen.queryByRole('dialog', { name: 'sign Rename DEMO-0043' })).toBeNull()
    const inline = within(dialog).getByRole('region', { name: 'sign Rename DEMO-0043' })
    expect(inline).toHaveAttribute('data-confirm', 'Rename')
    expect(within(inline).getByTestId('rename-diff')).toHaveTextContent('Load tariff tables as dbt seeds → Load tariff tables as dbt seeds v2')
    expect(within(dialog).getByLabelText('Title')).toHaveAttribute('readonly') // the title is read-only while signing (run-11 P08)
    // the signed content is exactly the title change: nothing else rides along
    for (const k of ['sections', 'criteria', 'added', 'links', 'people', 'owner', 'assignees', 'reviewers']) expect(sent).not.toHaveProperty(k)
  })
  it('the rename says so when the title is unchanged, and offers the full form', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await user.click(await screen.findByRole('button', { name: /^Change the title: Load tariff tables/ }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Rename DEMO-0043' }, T)
    await user.click(within(dialog).getByRole('button', { name: 'Rename' }))
    expect(await within(dialog).findByText('The title is the same as before.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'More fields…' }))
    expect(await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)).toBeInTheDocument()
  })
  it('edits text, a criterion and the people, then asks for ONE signature', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    expect(within(dialog).getByRole('button', { name: 'Review and sign' })).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('New acceptance criterion'), 'A header row is exported')
    await user.click(within(dialog).getByRole('button', { name: 'Add' }))
    await user.click(within(dialog).getByRole('button', { name: /^Review and sign \(1 change\)$/ }))
    // the full edit is not a rename: no inline review, no Rename button, no old -> new line, and its fields cannot change while signing
    const review = await screen.findByRole('dialog', { name: 'sign Edit DEMO-0043' })
    expect(review).not.toHaveAttribute('data-confirm')
    expect(screen.queryByTestId('rename-diff')).toBeNull()
    expect(within(dialog).getByTestId('edit-fields')).toBeDisabled()
    expect(within(dialog).getByLabelText('Title')).toBeDisabled()
    const sent = JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
    expect(sent).toMatchObject({ action: 'edit_sequence', key: 'DEMO-0043', acceptance: { edit: {}, add: ['A header row is exported'] } })
    expect(typeof sent.head_seq).toBe('number')
    await user.click(screen.getByRole('button', { name: 'signed' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit DEMO-0043' })).toBeNull(), T)
  })
  it('says so when nothing changed', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    await user.click(within(dialog).getByRole('button', { name: 'Review and sign' }))
    expect(await within(dialog).findByText('Nothing has changed yet.')).toBeInTheDocument()
    expect(screen.queryByTestId('sent-action')).toBeNull()
  })
  it('K10: Cancel asks before discarding changes, and closes at once when nothing changed', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
    let dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit DEMO-0043' })).toBeNull(), T)
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
    dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    await user.type(within(dialog).getByLabelText('New acceptance criterion'), 'Something')
    await user.click(within(dialog).getByRole('button', { name: 'Add' }))
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    const ask = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' }, T)
    await user.click(within(ask).getByRole('button', { name: 'Keep editing' }))
    expect(screen.getByRole('dialog', { name: 'Edit DEMO-0043' })).toBeInTheDocument()
    await user.click(within(screen.getByRole('dialog', { name: 'Edit DEMO-0043' })).getByRole('button', { name: 'Cancel' }))
    await user.click(within(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' }, T)).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Edit DEMO-0043' })).toBeNull(), T)
  })
})

describe('buildEdit: repositories (F6)', () => {
  const linked = { ...ticket, links: { repos: ['api'], branches: { api: 'feat/old' } } }
  const wantLinks = (repos: string[], branches: Record<string, string>) => ({ ...none, links: { repos, branches } })
  it('is nothing when the links are as they were (an empty branch means none)', () => {
    expect(buildEdit(linked, wantLinks(['api'], { api: 'feat/old' }), true)).toEqual({ nothing: true })
    expect(buildEdit(ticket, wantLinks([], {}), true)).toEqual({ nothing: true })
    expect(buildEdit(ticket, none, true)).toEqual({ nothing: true }) // links never touched
  })
  it('links a repository with a branch as one change, and unlinking drops its branch', () => {
    expect(buildEdit(ticket, wantLinks(['web', 'api'], { web: ' feat/demo-0001 ', api: '' }), true)).toEqual({
      steps: 1,
      action: { action: 'edit_sequence', key: 'DEMO-0001', head_seq: 7, links: { repos: ['api', 'web'], branches: { web: 'feat/demo-0001' } } },
    })
    expect(buildEdit(linked, wantLinks([], {}), true)).toMatchObject({ action: { links: { repos: [], branches: {} } } })
    expect(buildEdit(linked, wantLinks(['api'], { api: 'feat/new' }), true)).toMatchObject({ action: { links: { repos: ['api'], branches: { api: 'feat/new' } } } })
  })
  it('counts the links as one of the changes of the review', () => {
    expect(buildEdit(ticket, { ...wantLinks(['api'], {}), added: ['One more'] }, true)).toMatchObject({ steps: 2 })
  })
  it('tells a bad branch name in words, naming the repository', () => {
    for (const bad of ['has space', '-x', 'a..b', 'x/', '/x', 'HEAD', 'refs/heads/x', 'a.lock', 'a~b', 'x@{y}']) {
      expect(buildEdit(ticket, wantLinks(['api'], { api: bad }), true), bad).toMatchObject({ problem: expect.stringContaining('Branch for api:') })
    }
    expect(buildEdit(ticket, wantLinks(['api'], { api: 'feature/ok-1.2' }), true)).toHaveProperty('action')
  })
})

describe('the repositories control in the edit dialog', () => {
  it('links a repository from the workspace list with the suggested branch, in the ONE review', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    const region = within(dialog).getByTestId('edit-repositories')
    const picker = await within(region).findByLabelText('Repository to link', undefined, T)
    const name = (picker as HTMLSelectElement).value
    expect(name).not.toBe('')
    await user.click(within(region).getByRole('button', { name: 'Link repository' }))
    const branch = within(region).getByLabelText(`Branch in ${name}`) as HTMLInputElement
    expect(branch.value).toBe('feat/demo-0043')
    await user.clear(branch)
    await user.type(branch, 'feat/shiny')
    await user.click(within(dialog).getByRole('button', { name: /^Review and sign \(1 change\)$/ }))
    const sent = JSON.parse((await screen.findByTestId('sent-action')).textContent ?? '{}')
    expect(sent).toMatchObject({ action: 'edit_sequence', key: 'DEMO-0043', links: { repos: expect.arrayContaining([name]), branches: { [name]: 'feat/shiny' } } })
  })
  it('refuses a bad branch name before anything is sent, and unlinking removes the row', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    const region = within(dialog).getByTestId('edit-repositories')
    await within(region).findByLabelText('Repository to link', undefined, T)
    await user.click(within(region).getByRole('button', { name: 'Link repository' }))
    const branch = within(region).getAllByLabelText(/^Branch in /)[0]
    await user.clear(branch)
    await user.type(branch, 'has space')
    expect(await within(region).findByRole('alert')).toHaveTextContent('cannot contain spaces')
    await user.click(within(dialog).getByRole('button', { name: /^Review and sign/ }))
    expect(await within(dialog).findByText(/Branch for .*cannot contain spaces/)).toBeInTheDocument()
    expect(screen.queryByTestId('sent-action')).toBeNull()
    const before = within(region).getAllByLabelText(/^Branch in /).length
    await user.click(within(region).getAllByRole('button', { name: /^Unlink / })[0])
    expect(within(region).queryAllByLabelText(/^Branch in /)).toHaveLength(before - 1)
  })
})

const task = { id: 'T1', text: 'Open it', verify: { cmd: 'check' }, proves: ['AC1'], state: 'todo' as const, lease: null, receipt: null, done_at: null }
const withTask = { ...ticket, tasks_state: [task] }
const draft = { text: 'Open it', verify: 'check', proves: ['AC1'] }
describe('buildEdit tasks', () => {
  it('sends only changed fields, including an explicit removal', () => {
    expect(buildEdit(withTask, { ...none, tasks: { edit: { T1: draft }, add: [] } }, true)).toEqual({ nothing: true })
    const made = buildEdit(withTask, { ...none, tasks: { edit: { T1: { ...draft, text: 'Open again', verify: '' } }, add: [] } }, true)
    expect(made).toMatchObject({ steps: 1 })
    expect('action' in made && made.action.tasks).toEqual({ edit: { T1: { text: 'Open again', verify: null } }, add: [] })
  })
  it('refuses changed checks or criteria on done tasks but allows text', () => {
    const done = { ...withTask, tasks_state: [{ ...task, state: 'done' as const }] }
    for (const change of [{ verify: '' }, { proves: [] }]) expect(buildEdit(done, { ...none, tasks: { edit: { T1: { ...draft, ...change } }, add: [] } }, true)).toMatchObject({ problem: expect.stringContaining('Reopen it first.') })
    expect(buildEdit(done, { ...none, tasks: { edit: { T1: { ...draft, text: 'Clearer' } }, add: [] } }, true)).toMatchObject({ steps: 1 })
  })
  it('counts edits and additions toward the shared 16 step limit', () => {
    const tasks = { edit: { T1: { ...draft, text: 'Clearer' } }, add: Array.from({ length: 15 }, () => draft) }
    expect(buildEdit(withTask, { ...none, tasks }, true)).toMatchObject({ steps: 16 })
    expect(buildEdit(withTask, { ...none, tasks, added: ['Another criterion'] }, true)).toHaveProperty('problem')
  })
  it('refuses empty and overlong task text and overlong checks', () => {
    for (const change of [{ text: '' }, { text: 'a'.repeat(501) }, { verify: 'é'.repeat(2049) }]) expect(buildEdit(withTask, { ...none, tasks: { edit: {}, add: [{ ...draft, ...change }] } }, true)).toHaveProperty('problem')
  })
})

it('adds a task from the Tasks block and builds the signing action', async () => {
  const { user } = renderApp('/ticket/DEMO-0043')
  await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
  const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
  await user.click(within(dialog).getByRole('button', { name: 'Add task' }))
  await user.type(within(dialog).getByLabelText('Text of new task 1'), 'Check the header')
  await user.type(within(dialog).getByLabelText('Check command of new task 1'), 'check-header')
  await user.click(within(dialog).getByLabelText('new task 1 proves AC1'))
  await user.click(within(dialog).getByRole('button', { name: /^Review and sign \(1 change\)$/ }))
  expect(JSON.parse((await screen.findByTestId('sent-action')).textContent!)).toMatchObject({ tasks: { edit: {}, add: [{ text: 'Check the header', verify: 'check-header', proves: ['AC1'] }] } })
})

it.each(['AC1', 'T1'])('opens the shared dialog from the %s pencil and focuses its text', async (id) => {
  const { user } = renderApp('/ticket/DEMO-0043?tab=acceptance')
  await user.click(await screen.findByRole('button', { name: `Edit ${id}` }, T))
  const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
  await waitFor(() => expect(within(dialog).getByLabelText(`Text of ${id}`)).toHaveFocus())
  if (id === 'T1') {
    expect(within(dialog).getByLabelText('Text of T1')).toBeEnabled()
    expect(within(dialog).getByLabelText('Check command of T1')).toBeDisabled()
    expect(within(dialog).getByLabelText('T1 proves AC1')).toBeDisabled()
  }
})

const displayTicket = { ...withTask, status: 'open', artifacts: [], gates: { verify: { state: 'waiting' } } } as unknown as TicketDocument
const editViewer: Viewer = { person: 'p_a', role: 'member', members: [], name: id => id ?? '', ready: true }
it.each([
  { status: 'open', seq: 7, role: 'member', host: true, offered: true },
  { status: 'done', seq: 7, role: 'member', host: true, offered: false },
  { status: 'open', seq: 0, role: 'member', host: true, offered: false },
  { status: 'open', seq: 7, role: 'viewer', host: true, offered: false },
  { status: 'open', seq: 7, role: 'member', host: false, offered: false },
] as const)('offers the overview and row buttons under the same rule: %j', ({ status, seq, role, host, offered }) => {
  vi.mocked(handoffMode).mockReturnValue(host)
  const current = { ...displayTicket, status, head: { ...displayTicket.head, seq } }
  const viewer = { ...editViewer, role }
  expect(canEditTicket(current, role, host)).toBe(offered)
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } })
  client.setQueryData(['workspaces'], [{ id: 'w_1', prefix: 'DEMO' }])
  render(<QueryClientProvider client={client}><TicketEditProvider ticket={current} viewer={viewer}>
    <EditTicket ticket={current} viewer={viewer} />
    <AcceptanceTasks ticket={current} viewer={viewer} jump={() => undefined} sign={() => undefined} />
  </TicketEditProvider></QueryClientProvider>)
  for (const name of ['Edit text, checks, repositories and people', 'Edit AC1', 'Edit T1']) {
    expect(screen.queryByRole('button', { name }) !== null).toBe(offered)
  }
})
