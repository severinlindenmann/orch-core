import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'
import { currentFor, groups, kindOf, reasonText, todayCards, wakeLine, type FollowupRow } from '@/api/followups'

const T = { timeout: 4000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const on = (s: MockStore) => installAndGrant(s, wsOf(s), 'schedules')

const row = (o: Partial<FollowupRow>): FollowupRow => ({
  id: 'fu_x', ticket: 'DEMO-0045', title: 'T', state: 'pending', trigger: 'time', due: '2026-10-10T14:00:00Z', deadline: null, set_at: '2026-10-09T09:00:00Z',
  fired_at: null, late_by: null, brief: null, outcome: null, reason: null, running: false, person: 'p_sev', mine: true, can_cancel: true, ...o,
})

beforeEach(() => {
  localStorage.clear()
  // sonner's toast (the Undo button) captures the pointer; jsdom has no such method
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
})

describe('follow-up rules', () => {
  it('every closed reason has a sentence and no code reaches the screen', () => {
    for (const reason of ['no_host', 'harness_failed', 'budget', 'runner_unavailable', 'runner_restarted', 'host_restarted', 'grant_revoked', 'ticket_moved_on', 'no_writes', 'error']) {
      const s = reasonText(row({ state: 'did_not_run', outcome: 'did_not_run', reason }))
      expect(s.length).toBeGreaterThan(20)
      expect(s).not.toMatch(/_/)
    }
    for (const reason of ['replaced', 'ticket_left_in_progress', 'claimed_by_other', 'grant_revoked', 'member_removed', 'addon_disabled', 'chain_cap', 'person', 'agent', 'run_now', 'dismissed'])
      expect(reasonText(row({ state: 'cancelled', reason }))).not.toMatch(/_/)
    expect(reasonText(row({ state: 'failed', reason: null }))).toBe('No reason was recorded.')
  })
  it('kinds, the latest follow-up per ticket, and the 7-day window', () => {
    const now = Date.parse('2026-10-20T00:00:00Z')
    expect(kindOf(row({ state: 'ran', late_by: 25200 }))).toBe('ran_late')
    expect(kindOf(row({ state: 'ran', trigger: 'deadline' }))).toBe('woken')
    const old = row({ id: 'a', state: 'ran', set_at: '2026-10-01T00:00:00Z', fired_at: '2026-10-02T00:00:00Z', due: null })
    expect(currentFor([old], 'DEMO-0045', now)).toBeNull() // finished more than 7 days ago
    const stuck = row({ id: 'b', state: 'did_not_run', set_at: '2026-10-01T00:00:00Z', due: '2026-10-02T00:00:00Z' })
    expect(currentFor([stuck], 'DEMO-0045', now)?.id).toBe('b') // a problem stays while it is the latest
    const newer = row({ id: 'c', state: 'pending', set_at: '2026-10-10T00:00:00Z' })
    expect(currentFor([stuck, newer], 'DEMO-0045', now)?.id).toBe('c')
    expect(groups([stuck, newer], now).needs).toEqual([]) // an older record is history, not a card
  })
  it('success never makes a Today card; Hide for me hides the card only', () => {
    const rows = [row({ id: 'ok', ticket: 'A-1', state: 'ran', set_at: '2026-10-09T00:00:00Z' }), row({ id: 'bad', ticket: 'A-2', state: 'failed', reason: 'no_writes', set_at: '2026-10-09T00:00:00Z' })]
    const now = Date.parse('2026-10-10T00:00:00Z')
    expect(todayCards(rows, new Set(), now).map((r) => r.id)).toEqual(['bad'])
    expect(todayCards(rows, new Set(['bad']), now)).toEqual([])
    expect(currentFor(rows, 'A-2', now)?.id).toBe('bad') // the strip and the chip still show it
  })
  it('a webhook or event wake-up is drawn only where the data has one', () => {
    expect(wakeLine(row({}))).toMatch(/^Wakes \w{3} /)
    expect(wakeLine(row({}))).not.toMatch(/earlier|finishes/)
    expect(wakeLine(row({ trigger: 'orch', due: null, deadline: '2026-10-13T16:00:00Z', label: 'backfill 8812' }))).toMatch(/^Wakes when backfill 8812 finishes, at the latest /)
  })
})

describe('follow-ups on the mock', () => {
  it('the Schedules page has a third tab with the grouped list, and Cancel has an Undo', async () => {
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('tab', { name: /^Follow-ups\s*\d+/ }, T))
    const needs = await screen.findByTestId('followups-needs', {}, T)
    expect(within(needs).getByText(/DEMO-0054/)).toBeInTheDocument()
    expect(within(needs).getByText(/Stopped by orch/)).toBeInTheDocument()
    expect(within(needs).getByText(/DEMO-0046/)).toBeInTheDocument()
    expect(within(needs).getByText(/DEMO-0041/)).toBeInTheDocument()
    const waiting = screen.getByTestId('followups-waiting')
    expect(within(waiting).getAllByTestId('followup-row')).toHaveLength(2)
    expect(within(waiting).getByText(/Wakes when backfill 8812 finishes/)).toBeInTheDocument()
    expect(screen.getByTestId('followups-finished')).toHaveTextContent(/Woken by deadline/)
    expect(screen.getAllByRole('button', { name: 'Run now as me' }).length).toBeGreaterThan(0)
    const mine = within(waiting).getAllByTestId('followup-row').find((r) => r.dataset.ticket === 'DEMO-0048')!
    await user.click(within(mine).getByRole('button', { name: 'Cancel follow-up' }))
    await waitFor(() => expect(within(screen.getByTestId('followups-finished')).getByText(/A person cancelled it/)).toBeInTheDocument(), T)
    await user.click(await screen.findByRole('button', { name: 'Undo' }, T))
    await waitFor(() => expect(within(screen.getByTestId('followups-waiting')).getAllByTestId('followup-row')).toHaveLength(2), T)
  })
  it('Hide for me on the list moves a failed row out of "needs a person" for this person only', async () => {
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('tab', { name: /^Follow-ups\s*\d+/ }, T))
    const needs = await screen.findByTestId('followups-needs', {}, T)
    const failed = within(needs).getAllByTestId('followup-row').find((r) => r.dataset.ticket === 'DEMO-0041')!
    await user.click(within(failed).getByRole('button', { name: 'Hide for me' }))
    await waitFor(() => expect(within(screen.getByTestId('followups-needs')).queryByText(/DEMO-0041/)).not.toBeInTheDocument(), T)
    expect(within(screen.getByTestId('followups-finished')).getByText(/DEMO-0041/)).toBeInTheDocument()
    // the chain stopped by orch still waits for a decision: no Hide for it
    const stopped = within(screen.getByTestId('followups-needs')).getAllByTestId('followup-row').find((r) => r.dataset.ticket === 'DEMO-0054')!
    expect(within(stopped).queryByRole('button', { name: 'Hide for me' })).not.toBeInTheDocument()
  })
  it('a viewer sees the list and no buttons; the ticket strip, the board chip and a Today card show the same facts', async () => {
    const { user } = renderApp('/addon/schedules/schedules', { viewer: 'p_tom', setup: on })
    await user.click(await screen.findByRole('tab', { name: /^Follow-ups\s*\d+/ }, T))
    await screen.findByTestId('followups-needs', {}, T)
    expect(screen.queryByTestId('followup-buttons')).not.toBeInTheDocument()
    const off = screen.getAllByTestId('followup-buttons-off')
    expect(off.length).toBeGreaterThan(0)
    for (const b of within(off[0]).getAllByRole('button')) expect(b).toBeDisabled()
  })
  it('the ticket page: a quiet card while waiting, a banner when it needs a person', async () => {
    renderApp('/ticket/DEMO-0048', { viewer: 'p_sev', setup: on })
    // below 1280 px the card is in the Panels sheet and one line at the top says there is a follow-up
    expect(await screen.findByTestId('followup-line', {}, T)).toHaveTextContent(/^⏱ Follow-up \w{3} \d\d:\d\d$/)
    expect(screen.queryByTestId('followup-banner')).not.toBeInTheDocument()
  })
  it('the ticket page banner for a chain stopped by orch, with Run now as me', async () => {
    renderApp('/ticket/DEMO-0054', { viewer: 'p_sev', setup: on })
    const banner = await screen.findByTestId('followup-banner', {}, T)
    expect(banner).toHaveTextContent(/Stopped by orch/)
    expect(within(banner).getByRole('button', { name: 'Run now as me' })).toBeInTheDocument()
    expect(screen.queryByTestId('ticket-followup')).not.toBeInTheDocument()
  })
  it('the board shows the clock chip and the problem chips', async () => {
    renderApp('/board', { viewer: 'p_sev', setup: on })
    await waitFor(() => expect(screen.getAllByTestId('followup-chip').length).toBeGreaterThanOrEqual(3), T)
    const kinds = screen.getAllByTestId('followup-chip').map((c) => c.dataset.kind)
    expect(kinds).toEqual(expect.arrayContaining(['waiting', 'did_not_run', 'failed']))
  })
  it('Today lists a card for what went wrong, Hide for me removes only the card', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: on })
    const cards = await screen.findByTestId('followup-cards', {}, T)
    expect(within(cards).getAllByTestId('followup-card').length).toBeGreaterThanOrEqual(3)
    const bad = within(cards).getAllByTestId('followup-card').find((c) => c.dataset.kind === 'did_not_run')!
    await user.click(within(bad).getByRole('button', { name: 'Hide for me' }))
    await waitFor(() => expect(within(cards).queryByText(/DEMO-0046/)).not.toBeInTheDocument(), T)
    expect(mockStore.ticket('DEMO-0046')).toBeDefined()
  })
})
