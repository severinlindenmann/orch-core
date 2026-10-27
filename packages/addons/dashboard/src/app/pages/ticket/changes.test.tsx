import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { api, mockStore } from '@/api/client'
import { ApiError } from '@/api/types'
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
    expect(await screen.findByTestId('evidence-changes', {}, T)).toHaveTextContent(/the verdict covers commit c90e7a1/)
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
    expect(await screen.findByTestId('gate-notes', {}, T)).toHaveTextContent(/Verify: New commits after the verdict: [0-9a-f]{7}/)
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

  it('with the code review on, the next-step card offers it after the verdict', async () => {
    renderApp('/ticket/DEMO-0041', {
      setup: (s) => {
        s.appendWs(wsOf(s), { type: 'gate.policy_set', gate: 'code', approvers: 'maintainer', count: 1, not: 'assignees', applies: 'all' })
        const head = s.ticket('DEMO-0041')!.branch.head
        s.append('DEMO-0041', { type: 'verdict.given', actor: 'p_mara', result: 'pass', source_sha: head })
        s.append('DEMO-0041', { type: 'gate.approved', actor: 'p_mara', gate: 'verify', source_sha: head })
      },
    })
    expect(await within(await screen.findByTestId('next-step', {}, T)).findByRole('button', { name: 'Approve code review' })).toBeInTheDocument()
  })

  it('the verdict dialog signs the head it opened with: a push while it is open is refused as stale', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    const card = await screen.findByTestId('next-step', {}, T)
    await user.click(within(card).getByRole('button', { name: 'Give verdict' }))
    const dialog = await screen.findByRole('dialog', {}, T)
    await user.click(within(dialog).getByRole('radio', { name: /^Pass on c90e7a1/ }))
    mockStore.pushCommit('DEMO-0041')
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: /^Pass on c90e7a1/ })).toBeInTheDocument(), T)
    await user.click(within(dialog).getByRole('button', { name: 'Pass' }))
    expect(await within(dialog).findByRole('alert', {}, T)).toHaveTextContent(/New commits since you opened the verdict: the branch head is [0-9a-f]{7}, not c90e7a1/)
    expect(mockStore.ticket('DEMO-0041')!.verdict).toBeNull()
  })
})

// A real host reads one diff per person at a time and answers `quota.host` while busy (core #417): the tab says the
// host is busy and tries again; only after a few tries does it stop, still saying "busy", with a Try again button.
describe('ticket: Changes while the host is busy', () => {
  const busy = () => new ApiError(503, { code: 'quota.host', message: 'busy', retryable: true, retry_after_ms: 20 })

  it('says "busy, trying again" and then shows the diff', async () => {
    const real = api.getChanges.bind(api)
    let calls = 0
    const spy = vi.spyOn(api, 'getChanges').mockImplementation((...args) => (++calls <= 2 ? Promise.reject(busy()) : real(...args)))
    try {
      const { user } = renderApp('/ticket/DEMO-0042')
      await user.click(await screen.findByRole('tab', { name: /Changes/ }, T))
      expect(await screen.findByRole('status', {}, T)).toHaveTextContent('The host is busy reading other diffs; trying again')
      expect(await screen.findByLabelText('Diff', {}, T)).toBeInTheDocument()
      expect(screen.queryByText(/Could not load the diff/)).toBeNull()
      expect(calls).toBe(3)
    } finally {
      spy.mockRestore()
    }
  })

  it('stops after a few tries, still saying busy (not "could not load"), and Try again reads it again', async () => {
    let calls = 0
    const spy = vi.spyOn(api, 'getChanges').mockImplementation(() => {
      calls++
      return Promise.reject(busy())
    })
    try {
      const { user } = renderApp('/ticket/DEMO-0042')
      await user.click(await screen.findByRole('tab', { name: /Changes/ }, T))
      const alert = await screen.findByRole('alert', {}, T)
      expect(alert).toHaveTextContent('The host is still busy reading other diffs.')
      expect(alert).not.toHaveTextContent('Could not load')
      const before = calls
      expect(before).toBe(5) // the first read and four tries
      await user.click(within(alert).getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(calls).toBeGreaterThan(before), T)
    } finally {
      spy.mockRestore()
    }
  })
})

// #561 R7-M1: a diff git could not produce (failed or timed out) is "Diff unavailable", never an empty diff.
describe('ticket: a diff that cannot be read', () => {
  it('says "Diff unavailable" and draws no diff', async () => {
    const spy = vi.spyOn(api, 'getChanges').mockImplementation(() =>
      Promise.reject(new ApiError(409, { code: 'observe.unavailable', message: 'proj: diff unavailable: git could not produce it in time', retryable: false })),
    )
    try {
      const { user } = renderApp('/ticket/DEMO-0042')
      await user.click(await screen.findByRole('tab', { name: /Changes/ }, T))
      expect(await screen.findByRole('alert', {}, T)).toHaveTextContent('Diff unavailable')
      expect(screen.queryByLabelText('Diff')).toBeNull()
    } finally {
      spy.mockRestore()
    }
  })
})
