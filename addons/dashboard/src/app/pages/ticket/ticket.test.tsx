import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { findRail, openTicketPanel } from '@/test/ticketPanels'
import { GatesStrip } from './Gates'
import { WordDiff } from './History'

const T = { timeout: 5000 }

describe('ticket page', () => {
  it('shows DEMO-0043 with tasks T1-T4 and the open question Q2', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    expect(await screen.findByRole('heading', { level: 1, name: 'Load tariff tables as dbt seeds' }, T)).toBeInTheDocument()
    expect(screen.getByTestId('agent-status')).toHaveTextContent(/Claude Code is working for Severin/)
    expect(screen.getByTestId('gate-requirements')).toHaveAttribute('data-state', 'approved')

    await user.click(screen.getByRole('tab', { name: /Acceptance & tasks/ }))
    const states = ['T1', 'T2', 'T3', 'T4'].map((id) => document.getElementById(`task-${id}`)?.getAttribute('data-state'))
    expect(states).toEqual(['done', 'doing', 'doing', 'todo'])
    expect(document.getElementById('ac-AC1')).toHaveAttribute('data-state', 'evidenced')
    expect(within(document.getElementById('ac-AC1')!).getByRole('img', { name: 'Agent-asserted' })).toBeInTheDocument()
    expect(document.getElementById('ac-AC3')).toHaveAttribute('data-state', 'open')

    await user.click(screen.getByRole('tab', { name: /Questions/ }))
    expect(document.getElementById('question-Q2')).toHaveAttribute('data-state', 'open')
    expect(document.getElementById('question-Q1')).toHaveAttribute('data-state', 'answered')
  })

  it('answers Q2 here after a simulated Touch ID and updates the Questions tab', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(screen.getByRole('tab', { name: /Questions/ }))
    await user.click(await screen.findByRole('radio', { name: /DATE \(local midnight\)/ }))
    await user.click(within(document.getElementById('question-Q2')!).getByRole('button', { name: 'Answer Q2' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/sha256:/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Your answer: DATE/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Send answer' }))

    await waitFor(() => expect(document.getElementById('question-Q2')).toHaveAttribute('data-state', 'answered'), T)
    expect(within(document.getElementById('question-Q2')!).getByText(/Severin.*via dashboard.*Touch ID/)).toBeInTheDocument()
  })

  it('tells Tom that DEMO-0044 is not visible to him', async () => {
    renderApp('/ticket/DEMO-0044', { viewer: 'p_tom' })
    expect(await screen.findByText('This ticket is not visible to you', undefined, T)).toBeInTheDocument()
    expect(screen.queryByText('Rotate warehouse service credentials')).not.toBeInTheDocument()
  })

  it('shows an invalidated gate with its reason, and epic children', async () => {
    const view = renderApp('/ticket/DEMO-0046')
    expect(await screen.findByTestId('gate-plan', undefined, T)).toHaveAttribute('data-state', 'invalidated')
    expect(screen.getByTestId('gate-notes')).toHaveTextContent('Plan changed after approval: T2 added')
    view.unmount()

    renderApp('/ticket/DEMO-0040')
    expect(await screen.findByText(/Children \(/, undefined, T)).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /DEMO-0043/ }, T)).toBeInTheDocument()
  })

  it('renders DEMO-0041 (testing) with a verdict action for the reviewer', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    await user.click(screen.getByRole('tab', { name: /Acceptance & tasks/ }))
    expect(within(document.getElementById('ac-AC1')!).getByRole('img', { name: 'Proven by a receipt' })).toBeInTheDocument()
    expect(within(screen.getByTestId('ticket-header')).getByRole('button', { name: 'Give verdict' })).toBeInTheDocument()
  })

  it('renders added and removed words in the History changes view', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(screen.getByRole('tab', { name: 'History' }))
    await user.click(await screen.findByRole('radio', { name: 'Changes' }))
    await user.selectOptions(screen.getByLabelText('Section'), 'requirements')
    const diff = await screen.findByTestId('word-diff')
    expect(diff.querySelectorAll('ins').length).toBeGreaterThan(0)
    expect(diff.querySelectorAll('del').length).toBeGreaterThan(0)
  })
})

describe('a gate the person may not sign', () => {
  it('Mara sees "Approve plan (owners only)" disabled with the reason, and the turn line does not say ready to claim', async () => {
    renderApp('/ticket/DEMO-0044', { viewer: 'p_mara' })
    const button = await screen.findByRole('button', { name: 'Approve plan (owners only)' }, T)
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('Plan needs 1 approval from owners.')
    const header = screen.getByTestId('ticket-header')
    expect(header).toHaveTextContent(/plan needs approval \(owners\)/i)
    expect(header).not.toHaveTextContent(/ready to claim/i)
  })
  it('the owner gets the real button instead', async () => {
    renderApp('/ticket/DEMO-0044')
    expect(await screen.findByRole('button', { name: 'Approve plan' }, T)).toBeEnabled()
    expect(screen.queryByRole('button', { name: /owners only/ })).toBeNull()
  })
})

describe('ticket gate policy wording', () => {
  it('uses the same sentence as Settings, not the raw approver value', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'gate.policy_set', gate: 'plan', approvers: 'maintainer', count: 1, not: null }) })
    await user.click(await screen.findByTestId('gate-plan'))
    const plan = await screen.findByRole('dialog', { name: /Plan/ })
    expect(within(plan).getByText('Plan needs 1 approval from owners or maintainers.')).toBeInTheDocument()
    expect(screen.queryByText(/maintainer, 1 of 1/)).toBeNull()
  })
})

describe('WordDiff', () => {
  it('marks inserted and deleted words', () => {
    render(<WordDiff from="load the old tariffs" to="load the new tariffs today" />)
    const diff = screen.getByTestId('word-diff')
    expect(diff.querySelector('del')).toHaveTextContent('old')
    expect(diff.querySelector('ins')?.textContent).toMatch(/new/)
    expect(screen.getByTestId('diff-summary')).toHaveTextContent(/\+2 words.*−1 words/)
  })

  it('Tom gets no Claim button, and Start agent in Actions is disabled with the viewer reason', async () => {
    const { user } = renderApp('/ticket/DEMO-0043', { viewer: 'p_tom' })
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    expect(screen.queryByRole('button', { name: /^Claim/ })).toBeNull()
    await user.click(screen.getByRole('button', { name: /Actions/ }))
    const item = await screen.findByRole('menuitem', { name: /Start agent/ })
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(item).toHaveTextContent('Viewers cannot change tickets.')
  })

})

describe('ticket page structure: next action first, gates as a stepper, a rail that never drops', () => {
  afterEach(() => vi.unstubAllGlobals())
  const header = () => screen.getByTestId('ticket-header')

  it('at 1024 the verdict is a header button, there is no rail column, and Panels (N) opens a sheet', async () => {
    vi.stubGlobal('innerWidth', 1024)
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    expect(within(header()).getByRole('button', { name: 'Give verdict' })).toBeInTheDocument()
    expect(screen.queryByRole('complementary', { name: 'Ticket details' })).toBeNull()
    expect(screen.getByTestId('ticket-properties')).toHaveTextContent(/Size/)
    await user.click(screen.getByRole('button', { name: /^Panels \(\d+\)$/ }))
    const sheet = await screen.findByRole('dialog', { name: /Panels/ })
    expect(within(sheet).getByRole('heading', { name: 'Details' })).toBeInTheDocument()
  })

  it('at 1440 the rail is a column next to the content, without "Needs you"', async () => {
    vi.stubGlobal('innerWidth', 1440)
    renderApp('/ticket/DEMO-0041')
    const rail = await findRail()
    expect(within(header()).getByRole('button', { name: 'Give verdict' })).toBeInTheDocument()
    expect(within(rail).queryByText(/Needs you/)).toBeNull()
    expect(within(rail).queryByText(/^Head$/)).toBeNull()
    expect(screen.queryByTestId('ticket-properties')).toBeNull()
  })

  it('the primary action follows what the viewer has to do: Answer Q2 first', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(within(header()).getByRole('button', { name: 'Answer Q2' }))
    expect(await screen.findByRole('tab', { name: /Questions/, selected: true })).toBeInTheDocument()
  })

  it('a viewer gets no primary action', async () => {
    renderApp('/ticket/DEMO-0041', { viewer: 'p_tom' })
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    expect(within(header()).queryByRole('button', { name: /Give verdict|Answer|Approve/ })).toBeNull()
  })

  it('gates are one stepper of 3 buttons; a step opens its policy, signer and folded details', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    const gates = await screen.findByRole('group', { name: 'Gates' }, T)
    const steps = within(gates).getAllByRole('button')
    expect(steps).toHaveLength(3)
    expect(steps[0]).toHaveAccessibleName(/Requirements.*Approved/)
    await user.click(steps[1])
    const pop = await screen.findByRole('dialog', { name: /Plan/ })
    expect(within(pop).getByText('Plan needs 1 approval from owners.')).toBeInTheDocument()
    expect(within(pop).getByText(/Approved by Severin, 8 Oct/)).toHaveTextContent('Approved by Severin, 8 Oct · verified signature')
    expect(within(pop).getByText('Details')).toBeInTheDocument()
    expect(within(pop).getByText(/a7ee20379eb5/)).toBeInTheDocument()
  })

  it('an invalidated gate keeps its reason visible under the stepper', async () => {
    renderApp('/ticket/DEMO-0046')
    expect(await screen.findByTestId('gate-plan', undefined, T)).toHaveAttribute('data-state', 'invalidated')
    expect(screen.getByTestId('gate-notes')).toHaveTextContent('Plan changed after approval: T2 added')
  })

  it('shows no signature, hash or head jargon on the first level', async () => {
    renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/sig ok/)
    expect(text).not.toMatch(/hash [0-9a-f]{6}/)
    expect(text).not.toMatch(/seq \d+ ·/)
  })

  it('the agent is one status line, without Claim or Release buttons', async () => {
    renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    expect(screen.getByTestId('agent-status')).toHaveTextContent('Claude Code is working for Severin · since 08:05 UTC · 2 subagents')
    expect(screen.queryByRole('button', { name: /^Claim/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /^Release/ })).toBeNull()
  })

  it('a ticket nobody works on says so', async () => {
    renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    expect(screen.getByTestId('agent-status')).toHaveTextContent('No agent is working on this ticket')
  })

  it('a pull request appears once in the rail: the GitHub panel when it is active, core otherwise', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user, unmount } = renderApp('/ticket/DEMO-0043', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, 'Pull request')
    expect(await within(panel).findByText('#31', {}, T)).toBeInTheDocument()
    expect(within(await findRail()).getAllByText(/#31/)).toHaveLength(1)
    unmount()

    renderApp('/ticket/DEMO-0043', { viewer: 'p_sev', setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'addon.disabled', name: 'github' }) })
    const rail = await findRail()
    expect(await within(rail).findAllByText(/#31/, {}, T)).toHaveLength(1)
  })

  it('shows at most 3 labels and "+n"', async () => {
    renderApp('/ticket/DEMO-0046', { setup: (s) => void s.append('DEMO-0046', { type: 'labels.changed', add: ['alpha', 'beta', 'gamma'] }) })
    await screen.findByRole('heading', { level: 1 }, T)
    expect(within(header()).getAllByTestId('ticket-label')).toHaveLength(3)
    expect(within(header()).getByText('+3')).toBeInTheDocument()
  })
})

describe('Start agent from the ticket', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('is in the Actions menu with the addon marker', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    await screen.findByRole('heading', { level: 1 }, T)
    await user.click(screen.getByRole('button', { name: /Actions/ }))
    const item = await screen.findByRole('menuitem', { name: /Start agent/ })
    expect(within(item).getByRole('img', { name: /Start agent addon/ })).toBeInTheDocument()
    await user.click(item)
    expect(await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0044' }, T)).toBeInTheDocument()
  })

  it('on a claimed ticket Start is disabled with the reason, and no dialog opens', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0037', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, 'Start agent')
    const start = await within(panel).findByRole('button', { name: 'Start' }, T)
    expect(start).toBeDisabled()
    expect(start).toHaveAccessibleDescription(/DEMO-0037 is claimed by Codex for Mara\. Stop that session on Agents first\./)
    expect(within(panel).getByRole('alert')).toHaveTextContent(/claimed by Codex for Mara/)
    await user.click(screen.getByRole('button', { name: /Actions/ }))
    const item = await screen.findByRole('menuitem', { name: /Start agent/ })
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('dialog', { name: /Start/ })).toBeNull()
  })
})

describe('rail details and gate details (G3 review)', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('lists watchers, and a role held only by agents reads "none"', async () => {
    vi.stubGlobal('innerWidth', 1440)
    renderApp('/ticket/DEMO-0041', { setup: (s) => void s.append('DEMO-0041', { type: 'people.set', owner: 'p_sev', assignees: ['agent:codex'], reviewers: ['p_sev'], watchers: ['p_mara'] }) })
    const rail = await findRail()
    const row = (label: string) => within(rail).getByText(label).closest('div')!
    expect(row('Watchers')).toHaveTextContent('Mara')
    expect(row('Assignees')).toHaveTextContent('none')
  })

  it('says "unknown" for an approval without channel, presence or signature check, never a guessed default', async () => {
    const doc = structuredClone((await import('@/api/client')).mockStore.ticket('DEMO-0043')!)
    doc.gates.plan.approvals = [{ by: 'p_sev', at: '2026-10-08T09:44:00Z' }]
    const viewer = { person: 'p_sev', role: 'owner' as const, members: [{ person: 'p_sev', name: 'Severin', role: 'owner' as const }], name: (id: string | null | undefined) => (id === 'p_sev' ? 'Severin' : String(id)), ready: true }
    const user = (await import('@testing-library/user-event')).default.setup()
    render(<GatesStrip ticket={doc} viewer={viewer as never} />)
    await user.click(screen.getByTestId('gate-plan'))
    const pop = await screen.findByRole('dialog', { name: /Plan/ })
    expect(within(pop).getByText(/signature not checked/)).toBeInTheDocument()
    expect(within(pop).getByText(/via unknown · presence unknown/)).toBeInTheDocument()
    expect(within(pop).queryByText(/Touch ID|verified signature/)).toBeNull()
  })
})
