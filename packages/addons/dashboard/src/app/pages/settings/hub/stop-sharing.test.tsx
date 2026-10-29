// Stopping sharing (a ticket: share.revoke; a rule: share.rule_remove) is an unsigned owner action that only takes
// rights away: the C5 review (ticket, what stops, workspace prefix + short id, the caveat) and ONE red button that
// acts on a single trusted click (owner pick A, run5 P08: opening the dialog is the first press). No typed word, no terminal text, no sign flow.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import type { HubPreview, HubRule, HubShare } from '@/api/hub'
type HubShareRow = HubShare['rules'][number]
import * as gesture from '@/components/sign/trustedPress'
import { toast } from 'sonner'
import { ShareRules } from './ShareRules'
import { StopSharing } from './StopSharing'

const WS = { id: '0f3a9c2e-1111-2222-3333-44445555abcd', name: 'billing', prefix: 'DEMO' }
const NOW: HubPreview = {
  count: 2, new: 0, revoked: 0, more: 0, rules: [], plan: 'B'.repeat(43),
  items: [{ kind: 'ticket', line: 'ticket DEMO-0043: Export report as PDF' }, { kind: 'ticket', line: 'ticket DEMO-0045: Pricing page copy' }],
}
const AFTER: HubPreview = { ...NOW, count: 1, revoked: 1, plan: 'C'.repeat(43), items: [NOW.items[1]] }
const RULE: HubRule = { kind: 'type', value: 'feature' }

let revoke: Mock<typeof api.revokeHubShare>
let removeRule: Mock<typeof api.removeHubRule>
beforeEach(() => {
  vi.spyOn(api, 'getHubPreview').mockImplementation(async (_ws, body) => (body.remove ? AFTER : NOW))
  vi.spyOn(api, 'getHubShare').mockResolvedValue({
    available: true, shared: 2, refused: [], tickets: ['DEMO-0043'], tickets_complete: true,
    rules: [{ kind: 'type', value: 'feature', count: 1, describes: "every ticket of type 'feature'" }],
  })
  revoke = vi.fn<typeof api.revokeHubShare>().mockResolvedValue({ synced: true, pending: null, revoked: 1 })
  removeRule = vi.fn<typeof api.removeHubRule>().mockResolvedValue({ synced: true, pending: null, revoked: 1 })
  vi.spyOn(api, 'revokeHubShare').mockImplementation(revoke)
  vi.spyOn(api, 'removeHubRule').mockImplementation(removeRule)
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function mount(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}
const trusted = () => vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
async function ticketReview() {
  mount(<StopSharing ws={WS.id} workspace={WS} ticket="DEMO-0043" onClose={() => {}} />)
  const dialog = await screen.findByRole('dialog', { name: 'Stop sharing this ticket' })
  await waitFor(() => expect(within(dialog).getByTestId('hub-revoke-ticket')).toHaveTextContent('Export report as PDF'))
  return { dialog, button: within(dialog).getByTestId('hub-revoke-stop') }
}

describe('stop sharing one ticket', () => {
  it('the review is one sentence and the caveat; counts, workspace and "not signed" are behind Details', async () => {
    const { dialog } = await ticketReview()
    expect(within(dialog).getByTestId('hub-revoke-ticket')).toHaveTextContent('The hub stops showing the ticket “Export report as PDF”. 1 other item stays shared.')
    expect(within(dialog).getByTestId('hub-revoke-ticket').textContent).not.toMatch(/DEMO-0043|[0-9a-f]{8}|not signed/)
    expect(within(dialog).getByText(/cannot be taken back/)).toBeTruthy()
    expect(dialog.textContent).not.toMatch(/shared now|not signed|Workspace DEMO/)
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    expect(dialog.textContent).not.toMatch(/orch |terminal|backup key|type the/i)
    fireEvent.click(within(dialog).getByTestId('hub-revoke-details'))
    expect(within(dialog).getByTestId('hub-revoke-what')).toHaveTextContent('Stops being shared: 1 item.')
    expect(within(dialog).getByTestId('hub-revoke-what')).toHaveTextContent('2 items shared now · 1 after this')
    expect(within(dialog).getByTestId('hub-review-workspace')).toHaveTextContent('Workspace DEMO · 5555abcd · billing')
  })

  it('a single item reads "1 item", never "1 items"', async () => {
    vi.spyOn(api, 'getHubPreview').mockImplementation(async () => ({ ...NOW, count: 1, items: [NOW.items[0]] }))
    const { dialog } = await ticketReview()
    fireEvent.click(within(dialog).getByTestId('hub-revoke-details'))
    expect(within(dialog).getByTestId('hub-revoke-what')).toHaveTextContent('1 item shared now · 0 after this')
    expect(dialog.textContent).not.toMatch(/1 items/)
  })

  it('the confirm button is disabled until the preview has loaded', async () => {
    let release: (p: HubPreview) => void = () => {}
    vi.spyOn(api, 'getHubPreview').mockImplementation(() => new Promise(r => { release = r }))
    mount(<StopSharing ws={WS.id} workspace={WS} ticket="DEMO-0043" onClose={() => {}} />)
    const dialog = await screen.findByRole('dialog', { name: 'Stop sharing this ticket' })
    expect(within(dialog).getByTestId('hub-revoke-stop')).toBeDisabled()
    release(NOW)
    await waitFor(() => expect(within(dialog).getByTestId('hub-revoke-stop')).toBeEnabled())
  })

  it('one trusted click stops sharing once, closes the dialog and says "Stopped sharing" with the title', async () => {
    trusted()
    const ok = vi.spyOn(toast, 'success')
    const onClose = vi.fn()
    mount(<StopSharing ws={WS.id} workspace={WS} ticket="DEMO-0043" onClose={onClose} />)
    const dialog = await screen.findByRole('dialog', { name: 'Stop sharing this ticket' })
    const button = await within(dialog).findByTestId('hub-revoke-stop')
    await waitFor(() => expect(button).toBeEnabled())
    await waitFor(() => expect(within(dialog).getByTestId('hub-revoke-ticket')).toHaveTextContent('Export report as PDF'))
    expect(within(dialog).getByTestId('hub-flow-cancel')).toHaveTextContent('Keep sharing')
    vi.spyOn(api, 'getHubPreview').mockImplementation(async () => ({ ...NOW, count: 1, items: [NOW.items[1]] })) // the refresh: the ticket is gone
    fireEvent.click(button)
    fireEvent.click(button)
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(revoke).toHaveBeenCalledTimes(1)
    expect(revoke).toHaveBeenCalledWith(WS.id, 'DEMO-0043')
    expect(ok).toHaveBeenCalledWith('Stopped sharing “Export report as PDF”', { description: 'The hub has been asked to delete its copy.' })
  })

  it('when the hub could not be told the message says so', async () => {
    trusted()
    revoke.mockResolvedValue({ synced: false, pending: null, revoked: 1 })
    const ok = vi.spyOn(toast, 'success')
    const { button } = await ticketReview()
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(ok).toHaveBeenCalled())
    expect(ok.mock.calls[0][1]).toEqual({ description: 'Stopped here. The hub is told as soon as it can be reached.' })
  })

  it('says the ticket stays unshared even if a rule matches; never "may share it again"', async () => {
    const { dialog } = await ticketReview()
    expect(dialog.textContent).toContain('Rules that match it will not share it again.')
    expect(dialog.textContent).not.toMatch(/may share it again/)
  })

  it('an untrusted click on the ticket button does not call revoke', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(false)
    const { button } = await ticketReview()
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    expect(revoke).not.toHaveBeenCalled()
  })

  it('a refusal is said plainly', async () => {
    trusted()
    revoke.mockRejectedValue(new ApiError(403, { code: 'session.readonly', message: 'x', retryable: false }))
    const { dialog, button } = await ticketReview()
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Sign in fully on this computer to stop sharing.')
    expect(within(dialog).getByTestId('hub-revoke-stop')).toBeEnabled() // the dialog stays open to try again
  })
})

async function ruleReview() {
  const user = userEvent.setup()
  mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
  await user.click(await screen.findByTestId('hub-rule-remove-0'))
  const dialog = await screen.findByRole('dialog', { name: 'Stop sharing' })
  await within(dialog).findByTestId('hub-rule-review-what')
  return { dialog, button: within(dialog).getByTestId('hub-rule-stop') }
}

describe('a review over the host limits', () => {
  it('says the host\'s plain sentence even when the message carries a prefix', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getHubPreview').mockRejectedValue(new ApiError(400, { code: 'invalid.input', message: 'share.preview: This rule reaches too many people. Narrow it to share.', retryable: false }))
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-add'))
    await user.type(screen.getByTestId('hub-rule-value'), 'team')
    expect(await screen.findByRole('alert')).toHaveTextContent('This rule reaches too many people. Narrow it to share.')
    expect(screen.getByRole('alert').textContent).not.toMatch(/share\.preview/)
  })
})

describe('the shared list', () => {
  it('shows the ticket title, one Stop sharing per ticket, no "Shared ticket 1"', async () => {
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    const list = await screen.findByTestId('hub-ticket-list')
    await waitFor(() => expect(list).toHaveTextContent('Export report as PDF'))
    expect(within(list).getAllByRole('button', { name: 'Stop sharing' })).toHaveLength(1)
    expect(document.body.textContent).not.toMatch(/Shared ticket 1|Stop sharing this ticket/)
    expect(screen.getByText('All tickets of type feature · 1 ticket')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: 'Stop sharing' })).toHaveLength(2) // one rule row + one ticket row
  })
})

describe('stop sharing by a rule', () => {
  it('a rejected removal preview shows an alert and Try again in the dialog', async () => {
    const get = vi.spyOn(api, 'getHubPreview').mockImplementation(async (_ws, body) => { if (body.remove) throw new Error('x'); return NOW })
    const user = userEvent.setup()
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-remove-0'))
    const dialog = await screen.findByRole('dialog', { name: 'Stop sharing' })
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Could not preview this rule')
    get.mockImplementation(async () => AFTER)
    await user.click(within(dialog).getByTestId('hub-rule-preview-retry'))
    await waitFor(() => expect(within(dialog).getByTestId('hub-rule-stop')).toBeEnabled())
  })

  it('the review shows the rule, what stops, the workspace line and the caveat', async () => {
    const { dialog } = await ruleReview()
    expect(within(dialog).getByTestId('hub-rule-review-rule')).toHaveTextContent('What stops: feature tickets')
    const what = within(dialog).getByTestId('hub-rule-review-what')
    expect(what).toHaveTextContent('1 item stops being shared.')
    expect(what).toHaveTextContent('ticket DEMO-0043: Export report as PDF')
    expect(what).not.toHaveTextContent('DEMO-0045')
    fireEvent.click(within(dialog).getByTestId('hub-rule-details'))
    expect(within(dialog).getByTestId('hub-review-workspace')).toHaveTextContent('Workspace DEMO · 5555abcd · billing')
    expect(dialog.textContent).toContain('1 item shared after this.')
    expect(within(dialog).getByText(/cannot be taken back/)).toBeTruthy()
    expect(dialog.textContent).not.toMatch(/orch |terminal|One click is not ready/i)
  })

  it('one trusted click removes the rule once, bound to the plan that was shown; the dialog closes with a message', async () => {
    trusted()
    const ok = vi.spyOn(toast, 'success')
    const { button } = await ruleReview()
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Stop sharing' })).toBeNull())
    expect(removeRule).toHaveBeenCalledTimes(1)
    expect(removeRule).toHaveBeenCalledWith(WS.id, RULE, 'C'.repeat(43))
    expect(ok).toHaveBeenCalledWith('Stopped sharing feature tickets', { description: 'The hub has been asked to delete its copy.' })
  })

  it('the confirm is disabled until the preview plan exists; a synthetic click removes nothing', async () => {
    let release: (p: HubPreview) => void = () => {}
    vi.spyOn(api, 'getHubPreview').mockImplementation((_ws, body) => body.remove ? new Promise(r => { release = r }) : Promise.resolve(NOW))
    const user = userEvent.setup()
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-remove-0'))
    const dialog = await screen.findByRole('dialog', { name: 'Stop sharing' })
    expect(within(dialog).getByTestId('hub-rule-stop')).toBeDisabled()
    await waitFor(() => expect(api.getHubPreview).toHaveBeenCalledWith(WS.id, { remove: RULE }))
    release(AFTER)
    const button = await within(dialog).findByTestId('hub-rule-stop')
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button) // synthetic: trustedPress is false
    expect(removeRule).not.toHaveBeenCalled()
  })

  it('a removal that would also share new items lists them and offers no one-click button, only Keep sharing', async () => {
    const widening: HubPreview = { ...AFTER, count: 2, new: 1, items: [NOW.items[1], { kind: 'ticket', line: 'ticket DEMO-0050: Later public note' }] }
    vi.spyOn(api, 'getHubPreview').mockImplementation(async (_ws, body) => (body.remove ? widening : NOW))
    const user = userEvent.setup()
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-remove-0'))
    const dialog = await screen.findByRole('dialog', { name: 'Stop sharing' })
    const fresh = await within(dialog).findByTestId('hub-rule-review-new')
    expect(fresh).toHaveTextContent('1 newly shared')
    expect(fresh).toHaveTextContent('ticket DEMO-0050: Later public note')
    expect(within(dialog).getByTestId('hub-rule-widens')).toHaveTextContent("This change would also share new items, so it can't be done here yet.")
    expect(within(dialog).queryByTestId('hub-rule-stop')).toBeNull()
    expect(within(dialog).getByTestId('hub-flow-cancel')).toHaveTextContent('Keep sharing')
    expect(dialog.textContent).not.toMatch(/orch |terminal|command line/i)
    expect(removeRule).not.toHaveBeenCalled()
  })

  it('a removal that shares nothing new keeps the one-click button and lists nothing as newly shared', async () => {
    const { dialog, button } = await ruleReview()
    expect(button).toBeTruthy()
    expect(within(dialog).queryByTestId('hub-rule-review-new')).toBeNull()
    expect(within(dialog).queryByTestId('hub-rule-widens')).toBeNull()
  })

  it('adding a rule for nobody but the owner has no empty info icon', async () => {
    const user = userEvent.setup()
    vi.spyOn(api, 'getHubPreview').mockImplementation(async () => ({
      ...NOW, new: 1, plan: 'N'.repeat(43),
      review: { hub_id: 'ab12cd34ef56ab12', readers: { people: [], teams: [], note: null, info: null }, widened: [], new_count: 1, new_more: 0, new_items: [], leaves: [], later_edits: null },
    }))
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-add'))
    await user.type(screen.getByTestId('hub-rule-value'), 'team')
    await waitFor(() => expect(screen.getByTestId('hub-share-review')).toBeEnabled())
    await user.click(screen.getByTestId('hub-share-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Review sharing change' })
    expect(within(dialog).getByTestId('hub-add-readers')).toHaveTextContent('Only you can see it.')
    expect(within(dialog).queryByTestId('hub-add-readers-info')).toBeNull()
  })

  it('adding a rule: the review is the host\'s words, only new items, one trusted press, no terminal, no typed word', async () => {
    const user = userEvent.setup()
    const add = vi.spyOn(api, 'addHubRule').mockResolvedValue({ synced: true, pending: null, put: 1 })
    const ok = vi.spyOn(toast, 'success')
    vi.spyOn(api, 'getHubPreview').mockImplementation(async () => ({
      ...NOW, new: 1, plan: 'N'.repeat(43),
      review: { hub_id: 'ab12cd34ef56ab12', readers: { people: ['Ben Test'], teams: [], note: 'from the hub, not signed', info: 'Team membership is managed by the hub admin.' }, widened: [{ line: 'ticket DEMO-0001: Old', people_added: ['Ana'], teams_added: [], description_added: false }], new_count: 1, new_more: 0,
        new_items: ['ticket DEMO-0050: Later public note'], leaves: ['the ticket title, status, labels and deadline'],
        later_edits: 'Later edits to these tickets are shared too, until you stop sharing them.' },
    }))
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-add'))
    await user.type(screen.getByTestId('hub-rule-value'), 'team')
    await user.click(await screen.findByTestId('hub-rule-person-' + 'b'.repeat(32))) // Ben Test, from the hub
    expect(screen.getByTestId('hub-rule-description')).toBeChecked() // the description goes along unless unticked
    await waitFor(() => expect(screen.getByTestId('hub-share-review')).toBeEnabled())
    await user.click(screen.getByTestId('hub-share-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Review sharing change' })
    expect(within(dialog).getByTestId('hub-add-readers')).toHaveTextContent('Who will see it: Ben Test')
    const wide = within(dialog).getByTestId('hub-add-widened')
    expect(wide).toHaveTextContent('Reaches more now: Old')
    expect(wide).toHaveTextContent('adds Ana')
    expect(within(dialog).queryByText('Team membership is managed by the hub admin.')).toBeNull() // behind the i
    await user.click(within(dialog).getByTestId('hub-add-readers-info'))
    expect(await screen.findByText('Team membership is managed by the hub admin.')).toBeTruthy()
    await user.keyboard('{Escape}')
    expect(within(dialog).getByTestId('hub-add-later')).toHaveTextContent('Later edits to these tickets are shared too')
    expect(within(dialog).queryByTestId('hub-add-new')).toBeNull() // what leaves is behind the i (r10-P08)
    await user.click(within(dialog).getByTestId('hub-add-details'))
    expect(await screen.findByTestId('hub-add-new')).toHaveTextContent('1 new item leaves')
    expect(screen.queryByTestId('hub-add-list')).toBeNull() // the list is behind one more small click
    await user.click(screen.getByText('Show list'))
    expect(screen.getByTestId('hub-add-list')).toHaveTextContent('Later public note')
    await user.keyboard('{Escape}')
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    expect(dialog.textContent).not.toMatch(/orch |terminal|backup key|type the/i)
    // a script-made click is not a press
    const press = vi.spyOn(gesture, 'trustedPress').mockReturnValue(false)
    fireEvent.click(within(dialog).getByTestId('hub-add-go'))
    expect(add).not.toHaveBeenCalled()
    press.mockReturnValue(true)
    fireEvent.click(within(dialog).getByTestId('hub-add-go'))
    await waitFor(() => expect(add).toHaveBeenCalledWith(WS.id, { kind: 'type', value: 'team', audience: { users: ['b'.repeat(32)], groups: [] }, description: true }, 'N'.repeat(43)))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Review sharing change' })).toBeNull())
    expect(ok).toHaveBeenCalledWith('Shared 1 item with the hub.', { description: undefined })
    expect(screen.queryByTestId('hub-rule-value')).toBeNull() // the form is reset
  })

  it('a long review shows the first names and "and N more", the whole list behind the i, and nothing is cut', async () => {
    const user = userEvent.setup()
    const people = Array.from({ length: 12 }, (_, i) => 'Person ' + String(i + 1).padStart(2, '0'))
    const row = (n: number) => ({ line: `ticket DEMO-00${n}: Old ${n}`, people_added: people, teams_added: ['finance'], description_added: false })
    vi.spyOn(api, 'getHubPreview').mockImplementation(async () => ({
      ...NOW, new: 1, plan: 'N'.repeat(43),
      review: { hub_id: 'ab12cd34ef56ab12', readers: { people, teams: ['finance'], note: 'from the hub, not signed', info: null }, widened: [1, 2, 3, 4, 5].map(row), new_count: 1, new_more: 0,
        new_items: ['ticket DEMO-0050: Later public note'], leaves: [], later_edits: null },
    }))
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-add'))
    await user.type(screen.getByTestId('hub-rule-value'), 'team')
    await waitFor(() => expect(screen.getByTestId('hub-share-review')).toBeEnabled())
    await user.click(screen.getByTestId('hub-share-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Review sharing change' })
    expect(within(dialog).getByTestId('hub-add-readers')).toHaveTextContent('Who will see it: Person 01, Person 02, Person 03, Person 04, Person 05 and 8 more')
    expect(within(dialog).getAllByTestId('hub-add-widened')).toHaveLength(3)
    expect(within(dialog).getByTestId('hub-add-widened-more')).toHaveTextContent('and 2 more tickets reach more people')
    expect(within(dialog).getAllByTestId('hub-add-widened')[0]).toHaveTextContent('adds Person 01, Person 02, Person 03, Person 04, Person 05 and 8 more')
    await user.click(within(dialog).getByTestId('hub-add-readers-info'))
    expect(await screen.findByTestId('hub-add-readers-all')).toHaveTextContent('Person 12, team finance')
    await user.keyboard('{Escape}')
    await user.click(within(dialog).getByTestId('hub-add-details'))
    const all = await screen.findByTestId('hub-add-widened-all')
    expect(all.querySelectorAll('p')).toHaveLength(5)
    expect(all.textContent).toContain('Old 5')
    expect(all.textContent).toContain('Person 12')
  })

  it('a rule that reaches too many people shows the host\'s words and cannot be reviewed', async () => {
    const user = userEvent.setup()
    const tooMany = 'This rule reaches too many people. Narrow it to share.'
    vi.spyOn(api, 'getHubPreview').mockRejectedValue(new ApiError(422, { code: 'invalid.input', message: tooMany, retryable: false }))
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-add'))
    await user.type(screen.getByTestId('hub-rule-value'), 'team')
    expect(await screen.findByText(tooMany)).toBeTruthy()
    expect(screen.getByTestId('hub-share-review')).toBeDisabled()
  })

  it('an older host without the review says so and offers no button', async () => {
    const user = userEvent.setup()
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-add'))
    await user.type(screen.getByTestId('hub-rule-value'), 'team')
    await waitFor(() => expect(screen.getByTestId('hub-share-review')).toBeEnabled())
    await user.click(screen.getByTestId('hub-share-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Review sharing change' })
    expect(within(dialog).getByTestId('hub-add-unsupported')).toBeTruthy()
    expect(within(dialog).queryByTestId('hub-add-go')).toBeNull()
  })
})

describe('run 9 (J03): a one-ticket rule shared with its description', () => {
  const STORED: HubRule = { kind: 'ticket', value: 'PW-0001', audience: { users: ['a'.repeat(32)], groups: [] }, description: true }
  const ROW = { kind: 'ticket' as const, value: 'PW-0001', count: 1, describes: 'the ticket', readers: 1, audience: STORED.audience, description: true }
  const share = (rules: HubShareRow[]) => vi.spyOn(api, 'getHubShare').mockResolvedValue({ available: true, shared: 1, refused: [], tickets: ['PW-0001'], tickets_complete: true, rules })
  beforeEach(() => {
    share([ROW])
    vi.spyOn(api, 'listTickets').mockResolvedValue([{ key: 'PW-0001', title: 'Invoice export for Ben', type: 'feature', labels: ['billing'] }] as unknown as Awaited<ReturnType<typeof api.listTickets>>)
  })

  it('keeps the ticket row when a kind or label rule shares the ticket too', async () => {
    for (const other of [{ kind: 'type' as const, value: 'feature' }, { kind: 'tag' as const, value: 'billing' }]) {
      share([ROW, { ...other, count: 1, describes: 'x' }])
      mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
      const list = await screen.findByTestId('hub-ticket-list')
      expect(within(list).getAllByRole('listitem')).toHaveLength(1)
      cleanup()
    }
  })

  it('a row whose readers could not be read back cannot be stopped (never sent as a bare rule)', async () => {
    share([{ ...ROW, audience: null, removable: false }])
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    expect(await screen.findByTestId('hub-rule-remove-0')).toBeDisabled()
    expect(screen.getByTestId('hub-rule-unreadable-0')).toHaveTextContent('Could not read who sees this rule')
    expect(removeRule).not.toHaveBeenCalled()
    expect(within(await screen.findByTestId('hub-ticket-list')).getByTestId('hub-ticket-revoke-0')).toBeEnabled()  // the ticket stays stoppable
  })

  it('is listed once, and Stop sharing previews and removes the rule exactly as stored', async () => {
    trusted()
    const { dialog, button } = await ruleReview()
    expect(screen.queryByTestId('hub-ticket-list')).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Stop sharing' })).toHaveLength(1)
    expect(api.getHubPreview).toHaveBeenCalledWith(WS.id, { remove: STORED })
    await waitFor(() => expect(button).toBeEnabled())
    const ok = vi.spyOn(toast, 'success')
    fireEvent.click(button)
    await waitFor(() => expect(removeRule).toHaveBeenCalledWith(WS.id, STORED, 'C'.repeat(43)))
    await waitFor(() => expect(ok).toHaveBeenCalled())  // r7-P08: the dialog closes with a "Stopped sharing" toast
    void dialog
  })
})
