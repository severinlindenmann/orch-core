import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
import type { MockStore } from '@/mocks/store'

const T = { timeout: 8000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id

// Owner decision 2026-10-10 (1, 6): the verdict signs the commit; Testing shows the diff next to the evidence.
describe('ticket: the changes the verdict signs', () => {
  it('Testing: the Changes tab draws core\'s diff of the branch, and Acceptance links to it', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await user.click(await screen.findByRole('tab', { name: /Acceptance/ }, T))
    expect(await screen.findByTestId('evidence-changes', {}, T)).toHaveTextContent(/the verdict signs commit c90e7a1/)
    await user.click(within(screen.getByTestId('evidence-changes')).getByRole('button', { name: 'Open changes' }))
    const diff = await screen.findByLabelText('Diff', {}, T)
    expect(within(diff).getAllByText(/@@ -\d+/).length).toBeGreaterThan(0)
    expect(screen.getByRole('list', { name: 'Commits' })).toHaveTextContent('5be3d10')
  })

  it('a push after the verdict (demo): core voids it, the ticket is back in testing with the reason under the gates', async () => {
    const { user } = renderApp('/ticket/DEMO-0042')
    await user.click(await screen.findByRole('tab', { name: /Changes/ }, T))
    expect(await screen.findByText(/the verdict signed this commit/, {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Simulate: the agent pushes a commit' }))
    await waitFor(() => expect(mockStore.ticket('DEMO-0042')!.status).toBe('testing'), T)
    expect(await screen.findByTestId('gate-notes', {}, T)).toHaveTextContent(/Verification: New commits after the verdict: [0-9a-f]{7}/)
  })

  it('a viewer sees the diff but not the demo push', async () => {
    const { user } = renderApp('/ticket/DEMO-0042', { viewer: 'p_tom' })
    await user.click(await screen.findByRole('tab', { name: /Changes/ }, T))
    await screen.findByRole('list', { name: 'Commits' }, T)
    expect(screen.queryByRole('button', { name: /Simulate/ })).toBeNull()
  })

  it('a charter verdict says no person reviewed it', async () => {
    renderApp('/ticket/DEMO-0051', { setup: (s) => installAndGrant(s, wsOf(s), 'factory') })
    expect(await screen.findByTestId('gate-notes', {}, T)).toHaveTextContent('Verdict: via the factory charter — no person reviewed this (charter signed by Severin, commit')
  })

  it('with the code review on, the gates strip shows it after the verdict and the header offers it', async () => {
    renderApp('/ticket/DEMO-0041', {
      setup: (s) => {
        s.appendWs(wsOf(s), { type: 'gate.policy_set', gate: 'code', approvers: 'maintainer', count: 1, not: 'assignees', applies: 'all' })
        const head = s.ticket('DEMO-0041')!.branch.head
        s.append('DEMO-0041', { type: 'verdict.given', actor: 'p_mara', result: 'pass', source_sha: head })
        s.append('DEMO-0041', { type: 'gate.approved', actor: 'p_mara', gate: 'verify', source_sha: head })
      },
    })
    expect(await screen.findByTestId('gate-code', {}, T)).toHaveAccessibleName('Code review: Waiting for your review')
    expect(within(screen.getByTestId('ticket-header')).getByRole('button', { name: 'Approve code review' })).toBeInTheDocument()
  })
})
