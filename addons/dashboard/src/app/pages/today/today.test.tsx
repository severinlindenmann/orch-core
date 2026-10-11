import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', async (orig) => {
  const actual = await orig<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn(), loading: vi.fn() }) }
})

import { toast } from 'sonner'
import { ApiError } from '@/api/types'
import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { reloginItems } from '@/api/attention'

const T = { timeout: 6000 }
const busy = { setup: (s: typeof mockStore) => s.reset('busy', true) }
const group = (name: RegExp | string) => screen.findByRole('region', { name }, T)

describe('Today: a calm, grouped queue', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
  })

  it('says in one line what needs you and how many agents work, from the shared attention numbers', async () => {
    renderApp('/', { viewer: 'p_sev' })
    const line = await screen.findByText(/· \d+ need you · \d+ agents? working$/, {}, T)
    const ws = mockStore.workspaces[0].id
    const n = Number(/· (\d+) need you/.exec(line.textContent!)![1])
    // R-c: the owner's count includes the connections that need a new login, as listed.
    const relogin = reloginItems(mockStore.conn.connections(ws)).length
    expect(relogin).toBeGreaterThan(0)
    expect(n).toBe(mockStore.needsYou(ws).length + mockStore.addonDecisions(ws).length + relogin)
  })

  it('opens the first blocking question; picking an option posts nothing, "Send answer…" opens core\'s dialog', async () => {
    const post = vi.spyOn(api, 'postAction')
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const row = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, T)
    expect(within(row).getByText('blocking')).toBeInTheDocument()
    expect(within(row).getByText(/Asked by /)).toBeInTheDocument()
    const radios = await within(row).findAllByRole('radio', {}, T)
    expect(within(row).getByText('Recommended')).toBeInTheDocument()
    const send = within(row).getByRole('button', { name: 'Send answer…' })
    expect(send).toBeDisabled()
    await user.click(radios[1])
    expect(post).not.toHaveBeenCalled()
    await user.click(send)
    const dialog = await screen.findByRole('dialog', { name: /Answer Q2/ })
    await user.click(within(dialog).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('DEMO-0043', expect.objectContaining({ action: 'answer', question: 'Q2' })), T)
    await waitFor(() => expect(screen.queryByTestId('card-question:DEMO-0043:Q2')).not.toBeInTheDocument(), T)
    expect(toast.success).toHaveBeenCalled()
    expect(screen.queryByText(/undo isn't possible/)).toBeNull() // no stacked "Answered…" note cards
    post.mockRestore()
  })

  it('approvals open core\'s sign dialog from "Review"; verdicts from "Give verdict"; no Done / Changes / Copy hash', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const approvals = await group(/^Approvals · \d+/)
    await user.click(within(approvals).getAllByRole('button', { name: 'Review' })[0])
    expect(await screen.findByRole('dialog', { name: /Approve (plan|requirements)/ })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    const verdicts = await group(/^Verdicts · \d+/)
    await user.click(within(verdicts).getByRole('button', { name: 'Give verdict' }))
    const dialog = await screen.findByRole('dialog', { name: /Give a verdict/ })
    expect(within(dialog).getByRole('radio', { name: /Pass/ })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(within(verdicts).getByRole('link', { name: 'Evidence' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Done' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Changes' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy hash' })).toBeNull()
  })

  it('a failed answer shows the API message in core\'s dialog', async () => {
    const spy = vi.spyOn(api, 'postAction').mockRejectedValueOnce(new ApiError(403, { code: 'forbidden', message: 'Not yours to answer', hint: 'Ask the owner', retryable: false }))
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const row = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, T)
    await user.click((await within(row).findAllByRole('radio', {}, T))[0])
    await user.click(within(row).getByRole('button', { name: 'Send answer…' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send answer' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Not yours to answer')
    spy.mockRestore()
  })

  it('keeps a new question out of the list until "Show" (nothing moves under the pointer)', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const row = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, T)
    // The pill's slot is there before anything is new, first in the queue, and holds nothing yet.
    const slot = screen.getByTestId('new-items')
    const slotClass = slot.className
    expect(screen.getByRole('region', { name: 'Needs you' }).firstElementChild).toBe(slot)
    expect(within(slot).queryByRole('button')).toBeNull()
    mockStore.append('DEMO-0044', {
      type: 'question.asked',
      actor: 'p_mara',
      question: 'Q9',
      def: { id: 'Q9', to: 'p_sev', text: 'Keep the old role for a week?', options: [{ key: 'a', label: 'Yes' }, { key: 'b', label: 'No' }], recommended: 'a', blocking: true },
    })
    await user.click((await within(row).findAllByRole('radio', {}, T))[0])
    await user.click(within(row).getByRole('button', { name: 'Send answer…' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send answer' }))
    const pill = await screen.findByRole('button', { name: /^1 new · Show$/ }, T)
    expect(within(slot).getByRole('button', { name: /^1 new · Show$/ })).toBe(pill)
    expect(slot.className).toBe(slotClass) // the slot takes no space and does not change: nothing below it moves
    expect(screen.getByRole('region', { name: 'Needs you' }).firstElementChild).toBe(slot)
    expect(screen.queryByTestId('card-question:DEMO-0044:Q9')).toBeNull()
    await user.click(pill)
    expect(await screen.findByTestId('card-question:DEMO-0044:Q9')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new · Show/ })).toBeNull()
  })
})

describe('Today: addon decisions are core rows, signed in core', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
  })

  const openDecision = async (user: ReturnType<typeof renderApp>['user'], id: string) => {
    const row = await screen.findByTestId(`card-addon:${id}`, {}, T)
    const decide = within(row).queryByRole('button', { name: 'Decide' })
    if (decide?.getAttribute('aria-expanded') === 'false') await user.click(decide)
    return row
  }

  it('says once that you sign every answer in orch, with no per-row footer', async () => {
    renderApp('/', { viewer: 'p_sev' })
    const addons = await group(/^From addons · \d+/)
    expect(within(addons).getAllByText(/you sign every answer in orch/)).toHaveLength(1)
    expect(within(addons).queryByText(/requested by addon/)).toBeNull()
  })

  it('an option opens core\'s prompt; signing posts with the workspace id and core records addon.decided', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const success = vi.spyOn(toast, 'success')
    const { user, client } = renderApp('/', { viewer: 'p_sev' })
    const row = await openDecision(user, 'dec_publish_failed_build')
    client.setQueryData(['today', 'unrelated-workspace'], { untouched: true })
    client.setQueryData(['addon-state', 'unrelated-workspace', 'publish'], { untouched: true })
    const option = within(row).getByRole('button', { name: 'Retry last good version' })
    await user.click(option)
    const prompt = await screen.findByRole('dialog', { name: /^Decide for / })
    expect(within(prompt).getByText('Covers').nextElementSibling).toHaveTextContent('Answer: option retry')
    expect(within(prompt).getByRole('region', { name: 'From addon publish' })).toHaveTextContent('Retry last good version')
    expect(spy).not.toHaveBeenCalled()
    await user.click(within(prompt).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(spy).toHaveBeenCalled())
    const ws = mockStore.workspaces[0].id
    expect(spy.mock.calls[0][0]).toBe(ws)
    await waitFor(() => expect(mockStore.wsEventsOf(ws).some((e) => e.type === 'addon.decided' && e.id === 'dec_publish_failed_build' && e.presence === 'touchid')).toBe(true))
    await waitFor(() => expect(screen.queryByTestId('card-addon:dec_publish_failed_build')).toBeNull(), T)
    expect(client.getQueryState(['today', 'unrelated-workspace'])?.isInvalidated).toBe(false)
    expect(client.getQueryState(['addon-state', 'unrelated-workspace', 'publish'])?.isInvalidated).toBe(false)
    // The confirmation: core's sentence as the title, the addon's own message below it, labelled.
    expect(success).toHaveBeenCalledWith('Signed: answer retry · Publish (publish)', { description: expect.stringMatching(/^Addon says: /) })
    spy.mockRestore()
    success.mockRestore()
  })

  it('the success toast names the addon of the decision that was signed, not the one on screen now (F3)', async () => {
    const success = vi.spyOn(toast, 'success')
    const { user, client } = renderApp('/', { viewer: 'p_sev' })
    const row = await openDecision(user, 'dec_publish_failed_build')
    await user.click(within(row).getByRole('button', { name: 'Retry last good version' }))
    const prompt = await screen.findByRole('dialog', { name: /^Decide for / })
    // While the prompt is open the row (keyed by id) comes back from another addon: the snapshot still says Publish.
    const original = api.getAddonDecisions.bind(api)
    const list = vi.spyOn(api, 'getAddonDecisions').mockImplementation(async (ws) => (await original(ws)).map((d) => (d.id === 'dec_publish_failed_build' ? { ...d, addon: 'github' } : d)))
    await client.invalidateQueries({ queryKey: ['addon-decisions'] })
    await within(prompt).findByText(/This decision changed after you opened this prompt/, {}, T)
    list.mockRestore()
    await user.click(within(prompt).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(success).toHaveBeenCalled(), T)
    expect(success).toHaveBeenCalledWith('Signed: answer retry · Publish (publish)', { description: expect.stringMatching(/^Addon says: /) })
    success.mockRestore()
  })

  it('options are off from the click until the post resolves (no second prompt or post)', async () => {
    let release: () => void = () => {}
    const spy = vi.spyOn(api, 'runAddonAction').mockImplementation(() => new Promise((r) => (release = () => r({ ok: true, message: 'done' }))))
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const row = await openDecision(user, 'dec_publish_failed_build')
    await user.click(within(row).getByRole('button', { name: 'Retry last good version' }))
    await user.click(within(await screen.findByRole('dialog', { name: /^Decide for / })).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    for (const b of within(row).getAllByRole('button', { name: /Retry last good version|Leave it/ })) expect(b).toBeDisabled()
    release()
    expect(spy).toHaveBeenCalledTimes(1)
    spy.mockRestore()
  })

  it('cancelling the prompt posts nothing', async () => {
    const spy = vi.spyOn(api, 'runAddonAction')
    const { user } = renderApp('/', { viewer: 'p_sev' })
    const row = await openDecision(user, 'dec_publish_failed_build')
    await user.click(within(row).getByRole('button', { name: 'Leave it' }))
    await user.click(within(await screen.findByRole('dialog', { name: /^Decide for / })).getByRole('button', { name: 'Cancel' }))
    expect(spy).not.toHaveBeenCalled()
    expect(screen.getByTestId('card-addon:dec_publish_failed_build')).toBeInTheDocument()
    spy.mockRestore()
  })
})

describe('Today: busy day', { timeout: 20_000 }, () => {
  beforeEach(() => sessionStorage.clear())
  it('shows four groups of at most five rows, "Show N more", and folds the five quick-task asks', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', ...busy })
    for (const name of ['Questions', 'Approvals', 'Verdicts', 'From addons']) {
      const header = await screen.findByRole('button', { name: new RegExp(`^${name} · \\d+`) }, T)
      expect(header).toHaveAttribute('aria-expanded', 'true')
      const region = await group(new RegExp(`^${name} · `))
      expect(within(region).getAllByRole('listitem').filter((li) => li.parentElement?.closest('li') === null).length).toBeLessThanOrEqual(5)
    }
    expect(screen.getAllByRole('button', { name: /^Show \d+ more$/ }).length).toBeGreaterThanOrEqual(3)
    const fold = await screen.findByRole('button', { name: /^5 × .*outgrew/ }, T)
    expect(fold).toHaveAttribute('aria-expanded', 'false')
    await user.click(fold)
    expect(fold).toHaveAttribute('aria-expanded', 'true')
    const item = (await screen.findByText('Q-004 outgrew its limit: make it a ticket, or allow 3 more files?', {}, T)).closest('[data-testid^="card-addon:"]') as HTMLElement
    expect(within(item).getByRole('button', { name: 'Allow 3 more files' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Make it a ticket' })).toHaveLength(5)
  })

  it('a switch to the busy day starts a fresh queue, not a wave of "new" items', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev' })
    await screen.findByTestId('card-question:DEMO-0043:Q2', {}, T)
    await user.click(screen.getByRole('button', { name: 'Busy day' }))
    await user.click(await screen.findByRole('button', { name: 'Switch to busy day' }))
    expect(await screen.findByRole('button', { name: /^Verdicts · 9/ }, T)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /new · Show/ })).toBeNull()
    mockStore.sim.stopAll()
  })

  it('fold sub-rows name their ticket like any row', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', ...busy })
    await user.click(await screen.findByRole('button', { name: /^5 × AI Factory permit/ }, T))
    const subs = screen.getAllByTestId(/^card-addon:factory\.permit:/)
    expect(subs).toHaveLength(5)
    for (const sub of subs) expect(within(sub).getByRole('link', { name: /^DEMO-\d{4}$/ })).toBeInTheDocument()
  })

  it('collapses a group from its header', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', ...busy })
    const header = await screen.findByRole('button', { name: /^Verdicts · \d+/ }, T)
    await user.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(within(await group(/^Verdicts · /)).queryAllByRole('listitem')).toHaveLength(0)
  })
})

describe('Today by role', () => {
  it('Tom: nothing needs you, read-only rows without buttons, each names who decides', async () => {
    const { user } = renderApp('/', { viewer: 'p_tom' })
    expect(await screen.findByText(/Nothing needs you · \d+ open in the workspace/, {}, T)).toBeInTheDocument()
    // A viewer sees one summary per person who decides; the rows open on demand.
    for (const b of await screen.findAllByRole('button', { name: / decides · \d+ open/, expanded: false }, T)) await user.click(b)
    const row = await screen.findByTestId('card-question:DEMO-0043:Q2', {}, T)
    expect(within(row).queryAllByRole('button')).toHaveLength(0)
    expect(within(row).queryAllByRole('radio')).toHaveLength(0)
    expect(within(row).getByText(/decides$/)).toBeInTheDocument()
    for (const r of screen.getAllByTestId(/^card-/)) expect(within(r).queryAllByRole('button')).toHaveLength(0)
  })

  it('Mara: a last line says how many wait for an owner', async () => {
    renderApp('/', { viewer: 'p_mara' })
    expect(await screen.findByText(/\d+ more (is|are) waiting for an owner \(Severin\)/, {}, T)).toBeInTheDocument()
  })
})
