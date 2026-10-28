// Stopping sharing (a ticket: share.revoke; a rule: share.rule_remove) is an unsigned owner action that only takes
// rights away: the C5 review (ticket, what stops, workspace prefix + short id, the caveat) and ONE inline button that
// acts on a second trusted press within five seconds. No typed word, no terminal text, no sign flow.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { api } from '@/api/client'
import type { HubPreview, HubRule } from '@/api/hub'
import * as gesture from '@/components/sign/trustedPress'
import { ShareRules } from './ShareRules'
import { StopSharing } from './StopSharing'

const WS = { id: '0f3a9c2e-1111-2222-3333-44445555abcd', name: 'billing', prefix: 'DEMO' }
const NOW: HubPreview = {
  count: 2, new: 0, revoked: 0, more: 0, rules: [], plan: 'B'.repeat(43),
  items: [{ kind: 'ticket', line: 'ticket DEMO-0043: Export report as PDF' }, { kind: 'ticket', line: 'ticket DEMO-0045: Pricing page copy' }],
}
const AFTER: HubPreview = { ...NOW, count: 1, revoked: 1, plan: 'C'.repeat(43), items: [NOW.items[1]] }
const RULE: HubRule = { kind: 'type', value: 'feature' }
const T0 = 1_800_000_000_000

let revoke: Mock<typeof api.revokeHubShare>
let removeRule: Mock<typeof api.removeHubRule>
let clock: ReturnType<typeof vi.spyOn> | undefined
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
/** Press at `ms` after T0 (the control reads `Date.now`). */
function pressAt(button: HTMLElement, ms: number) {
  clock ??= vi.spyOn(Date, 'now')
  clock.mockReturnValue(T0 + ms)
  fireEvent.click(button)
}
afterEach(() => { clock = undefined })

async function ticketReview() {
  mount(<StopSharing ws={WS.id} workspace={WS} ticket="DEMO-0043" onClose={() => {}} />)
  const dialog = await screen.findByRole('dialog', { name: 'Stop sharing this ticket' })
  await within(dialog).findByTestId('hub-revoke-what')
  return { dialog, button: within(dialog).getByTestId('hub-revoke-stop') }
}

describe('stop sharing one ticket', () => {
  it('the review shows the ticket, what stops, the workspace prefix and short id, and the caveat; nothing to type', async () => {
    const { dialog } = await ticketReview()
    expect(within(dialog).getByTestId('hub-revoke-ticket')).toHaveTextContent('Ticket DEMO-0043 · Export report as PDF')
    expect(within(dialog).getByTestId('hub-revoke-what')).toHaveTextContent('(1 item)')
    expect(within(dialog).getByTestId('hub-revoke-what')).toHaveTextContent('2 items shared now · 1 after this')
    const line = within(dialog).getByTestId('hub-review-workspace')
    expect(line).toHaveTextContent('Workspace DEMO · 5555abcd · billing')
    expect(within(dialog).getByText(/cannot be taken back/)).toBeTruthy()
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    expect(dialog.textContent).not.toMatch(/orch |terminal|backup key|type the/i)
  })

  it('one press arms; a second within five seconds stops sharing once', async () => {
    trusted()
    const { dialog, button } = await ticketReview()
    pressAt(button, 0)
    expect(revoke).not.toHaveBeenCalled()
    expect(button).toHaveTextContent('Press again to stop sharing')
    pressAt(button, 4000)
    expect(revoke).toHaveBeenCalledTimes(1)
    expect(revoke).toHaveBeenCalledWith(WS.id, 'DEMO-0043')
    expect(await within(dialog).findByTestId('hub-revoke-stop-done')).toHaveTextContent('Stopped.')
    expect(within(dialog).queryByTestId('hub-revoke-stop')).toBeNull() // nothing left to press twice
  })

  it('two presses six seconds apart do not stop sharing', async () => {
    trusted()
    const { button } = await ticketReview()
    pressAt(button, 0)
    pressAt(button, 6000)
    expect(revoke).not.toHaveBeenCalled()
  })

  it('a synthetic click does nothing', async () => {
    const { button } = await ticketReview()
    pressAt(button, 0)
    pressAt(button, 1000)
    expect(revoke).not.toHaveBeenCalled()
    expect(button).toHaveTextContent('Stop sharing')
    expect(button).not.toHaveTextContent('Press again')
  })

  it('a refusal is said plainly', async () => {
    trusted()
    const { ApiError } = await import('@/api/types')
    revoke.mockRejectedValue(new ApiError(403, { code: 'session.readonly', message: 'x', retryable: false }))
    const { dialog, button } = await ticketReview()
    pressAt(button, 0)
    pressAt(button, 1000)
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Sign in fully on this computer to stop sharing.')
  })
})

async function ruleReview() {
  const user = userEvent.setup()
  mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
  await user.click(await screen.findByTestId('hub-rule-remove-0'))
  const review = screen.getByTestId('hub-share-review')
  await waitFor(() => expect(review).toBeEnabled())
  await user.click(review)
  const dialog = await screen.findByRole('dialog', { name: 'Review sharing change' })
  await within(dialog).findByTestId('hub-rule-review-what')
  return { dialog, button: within(dialog).getByTestId('hub-rule-stop') }
}

describe('stop sharing by a rule', () => {
  it('the review shows the rule, what stops, the workspace line and the caveat', async () => {
    const { dialog } = await ruleReview()
    expect(within(dialog).getByTestId('hub-rule-review-rule')).toHaveTextContent('Rule: type · feature')
    const what = within(dialog).getByTestId('hub-rule-review-what')
    expect(what).toHaveTextContent('1 no longer shared · 1 items shared after this')
    expect(what).toHaveTextContent('ticket DEMO-0043: Export report as PDF')
    expect(what).not.toHaveTextContent('DEMO-0045')
    expect(within(dialog).getByTestId('hub-review-workspace')).toHaveTextContent('Workspace DEMO · 5555abcd · billing')
    expect(within(dialog).getByText(/cannot be taken back/)).toBeTruthy()
    expect(dialog.textContent).not.toMatch(/orch |terminal|One click is not ready/i)
  })

  it('a second press within five seconds removes the rule once, bound to the plan that was shown', async () => {
    trusted()
    const { button } = await ruleReview()
    pressAt(button, 0)
    expect(removeRule).not.toHaveBeenCalled()
    pressAt(button, 4900)
    expect(removeRule).toHaveBeenCalledTimes(1)
    expect(removeRule).toHaveBeenCalledWith(WS.id, RULE, 'C'.repeat(43))
  })

  it('six seconds apart, or a synthetic click, removes nothing', async () => {
    const { button } = await ruleReview()
    pressAt(button, 0)
    pressAt(button, 100)
    expect(removeRule).not.toHaveBeenCalled() // synthetic
    trusted()
    pressAt(button, 1000)
    pressAt(button, 7000)
    expect(removeRule).not.toHaveBeenCalled()
  })

  it('adding a rule says plainly it is not available here, with no button and no terminal command', async () => {
    const user = userEvent.setup()
    mount(<ShareRules ws={WS.id} workspace={WS} onBack={() => {}} />)
    await user.click(await screen.findByTestId('hub-rule-add'))
    await user.type(screen.getByTestId('hub-rule-value'), 'team')
    await waitFor(() => expect(screen.getByTestId('hub-share-review')).toBeEnabled())
    await user.click(screen.getByTestId('hub-share-review'))
    const dialog = await screen.findByRole('dialog', { name: 'Review sharing change' })
    expect(within(dialog).getByTestId('hub-rule-add-unavailable')).toHaveTextContent('Adding a sharing rule is not available in the dashboard yet.')
    expect(within(dialog).queryByTestId('hub-rule-stop')).toBeNull()
    expect(dialog.textContent).not.toMatch(/orch |terminal/i)
  })
})
