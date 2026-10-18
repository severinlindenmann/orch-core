import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { buildEdit } from './EditTicket'

vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), handoffMode: () => true }))
// The signing dialog is its own feature (HandoffDialog tests); here it only shows the typed action it was given.
vi.mock('@/components/sign/HandoffDialog', () => ({
  HandoffDialog: (p: { title: string; action: unknown; onSettled?: (phase: string) => void; onClose: () => void }) => (
    <div role="dialog" aria-label={`sign ${p.title}`}>
      <pre data-testid="sent-action">{JSON.stringify(p.action)}</pre>
      <button onClick={() => p.onSettled?.('done')}>signed</button>
      <button onClick={p.onClose}>cancelled</button>
    </div>
  ),
}))

const T = { timeout: 5000 }
afterEach(() => vi.restoreAllMocks())

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

describe('the ticket page edit', () => {
  it('edits text, a criterion and the people, then asks for ONE signature', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(await screen.findByRole('button', { name: 'Edit text, checks, repositories and people' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Edit DEMO-0043' }, T)
    expect(within(dialog).getByRole('button', { name: 'Review and sign' })).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText('New acceptance criterion'), 'A header row is exported')
    await user.click(within(dialog).getByRole('button', { name: 'Add' }))
    await user.click(within(dialog).getByRole('button', { name: /^Review and sign \(1 change\)$/ }))
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
