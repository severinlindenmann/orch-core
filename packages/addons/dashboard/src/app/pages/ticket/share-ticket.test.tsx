// P06 (run 7): share one ticket with the hub from the ticket itself. The item shows only for the owner in share-some
// mode; the dialog is the Settings review (same preview, same plan), nothing typed.
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api } from '@/api/client'
import * as gesture from '@/components/sign/trustedPress'
import { setHubScenario, setMockOneClick, type HubScenario } from '@/mocks/hub'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
beforeEach(() => setMockOneClick(true))
afterEach(() => { cleanup(); setHubScenario(null); vi.restoreAllMocks() })

async function openMenu(scenario: HubScenario, opts: { viewer?: string; ticket?: string } = {}) {
  setHubScenario(scenario)
  const view = renderApp('/ticket/' + (opts.ticket ?? 'DEMO-0042'), opts)
  await view.user.click(await screen.findByRole('button', { name: /Actions/ }, T))
  return view
}

describe('the ticket Actions menu and sharing', () => {
  it('offers sharing in full hub mode too, says what the hub already holds, and shares with one click', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
    const add = vi.spyOn(api, 'addHubRule')
    const view = await openMenu('full')
    await view.user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
    await view.user.click(await screen.findByRole('button', { name: 'Share with a colleague' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Share this ticket' }, T)
    const go = await within(dialog).findByTestId('hub-add-go', {}, T)
    await waitFor(() => expect(go).toBeEnabled(), T)
    const boxes = within(dialog).getAllByRole('checkbox').filter(b => (b as HTMLElement).dataset.testid?.startsWith('hub-rule-person-'))
    if (boxes.length && !(boxes[0] as HTMLInputElement).checked) await view.user.click(boxes[0])
    expect(within(dialog).queryByTestId('hub-add-scope')).toBeNull() // P09: not in the main text
    await view.user.click(await within(dialog).findByTestId('hub-add-details', {}, T))
    await waitFor(() => expect(screen.getByTestId('hub-add-scope')).toHaveTextContent('The hub already holds this workspace'), T)
    await view.user.keyboard('{Escape}')
    await waitFor(() => expect(within(dialog).getByTestId('hub-add-go')).toBeEnabled(), T)
    fireEvent.click(within(dialog).getByTestId('hub-add-go'))
    await waitFor(() => expect(add).toHaveBeenCalledTimes(1), T)
  })

  it('offers "Share…" to the owner in share-some mode', async () => {
    await openMenu('selective')
    expect(await screen.findByRole('menuitem', { name: 'Share…' }, T)).toBeTruthy()
  })

  it('offers nothing in standalone mode', async () => {
    await openMenu('standalone')
    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByRole('menuitem', { name: /Share…|Stop sharing/ })).toBeNull()
  })

  it('offers nothing when the workspace has not joined a hub', async () => {
    await openMenu('nottold')
    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByRole('menuitem', { name: /Share…|Stop sharing/ })).toBeNull()
  })

  it('offers nothing on a closed ticket', async () => {
    setHubScenario('selective')
    const view = renderApp('/ticket/DEMO-0042', {
      setup: store => {
        const real = store.ticket.bind(store)
        vi.spyOn(store, 'ticket').mockImplementation(key => { const t = real(key); if (t && key === 'DEMO-0042') t.status = 'closed'; return t })
      },
    })
    await view.user.click(await screen.findByRole('button', { name: /Actions/ }, T))
    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByRole('menuitem', { name: /Share…|Stop sharing/ })).toBeNull()
  })

  it('offers nothing to a viewer who is not the owner', async () => {
    await openMenu('selective', { viewer: 'p_tom' })
    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByRole('menuitem', { name: /Share…|Stop sharing/ })).toBeNull()
  })

  it('shows the same review as Settings, binds its plan, and shares with one click', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
    const add = vi.spyOn(api, 'addHubRule')
    const ok = vi.spyOn(toast, 'success')
    const preview = vi.spyOn(api, 'getHubPreview')
    const view = await openMenu('selective')
    await view.user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
    await view.user.click(await screen.findByRole('button', { name: 'Share with a colleague' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Share this ticket' }, T)
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    const go = await within(dialog).findByTestId('hub-add-go', {}, T)
    await waitFor(() => expect(go).toBeEnabled(), T)
    expect(within(dialog).getByTestId('hub-add-readers')).toBeTruthy() // who will see it
    const shownPlan = await preview.mock.results[preview.mock.results.length - 1].value.then((p: { plan: string }) => p.plan)
    expect(preview).toHaveBeenLastCalledWith(expect.any(String), { add: { kind: 'ticket', value: 'DEMO-0042', description: true } })
    fireEvent.click(go)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Share this ticket' })).toBeNull(), T)
    expect(ok).toHaveBeenCalledWith(expect.stringMatching(/^Shared “.+” with the hub\.$|^Shared “.+” with .+\.$/), expect.anything())
    expect(add).toHaveBeenCalledTimes(1)
    expect(add.mock.calls[0][1]).toEqual({ kind: 'ticket', value: 'DEMO-0042', description: true })
    expect(add.mock.calls[0][2]).toBe(shownPlan)
  })

  it('an untrusted click shares nothing', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(false)
    const add = vi.spyOn(api, 'addHubRule')
    const view = await openMenu('selective')
    await view.user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
    await view.user.click(await screen.findByRole('button', { name: 'Share with a colleague' }, T))
    const go = await screen.findByTestId('hub-add-go', {}, T)
    await waitFor(() => expect(go).toBeEnabled(), T)
    fireEvent.click(go)
    expect(add).not.toHaveBeenCalled()
  })

  it('shows a Shared chip on the ticket once the hub has it (r10-P08)', async () => {
    await openMenu('selective', { ticket: 'DEMO-0043' })
    expect(await screen.findByTestId('shared-chip', {}, T)).toHaveTextContent('Shared')
  })

  it('says "Stop sharing" when the hub already has it, and opens the stop review', async () => {
    const view = await openMenu('selective', { ticket: 'DEMO-0043' }) // the mock hub already has DEMO-0043
    await view.user.click(await screen.findByRole('menuitem', { name: 'Stop sharing' }, T))
    expect(await screen.findByRole('dialog', { name: 'Stop sharing this ticket' }, T)).toBeTruthy()
  })

  it('the review is one sentence naming exactly the ticked people, the caveat once, and the counts behind Details (P05)', async () => {
    const view = await openMenu('selective')
    await view.user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
    await view.user.click(await screen.findByRole('button', { name: 'Share with a colleague' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Share this ticket' }, T)
    const go = await within(dialog).findByTestId('hub-add-go', {}, T)
    await waitFor(() => expect(go).toBeEnabled(), T)
    const text = () => dialog.textContent ?? ''
    // nobody ticked: it says only you, never "Who will see it:" and never "Only me"
    const boxes = within(dialog).getAllByRole('checkbox').filter(b => (b as HTMLElement).dataset.testid?.startsWith('hub-rule-person-') || (b as HTMLElement).dataset.testid?.startsWith('hub-rule-team-'))
    for (const b of boxes) if ((b as HTMLInputElement).checked) await view.user.click(b)
    await waitFor(() => expect(within(dialog).getByTestId('hub-add-sentence')).toHaveTextContent('Only you will see it.'), T)
    expect(text()).not.toMatch(/Who will see it:|Only me|not signed/)
    // tick one person: the sentence names them and what they see, in one place
    await view.user.click(boxes[0])
    await waitFor(() => expect(within(dialog).getByTestId('hub-add-sentence')).toHaveTextContent(/will see this ticket in their own workspace: the title, status, labels, deadline and description\. Later edits are shared too\./), T)
    expect(within(dialog).getByTestId('hub-add-readers')).not.toHaveTextContent('Only you')
    // P16: one row: Details on the left, Cancel and Share on the right; the warning and the long list are under Details
    const row = within(dialog).getByTestId('hub-add-go').parentElement as HTMLElement
    expect(within(row).getAllByRole('button').map(b => b.textContent)).toEqual(['Cancel', 'Share'])
    expect(row.parentElement).toContainElement(within(dialog).getByTestId('hub-add-details'))
    expect(within(dialog).getAllByRole('button', { name: 'Share' })).toHaveLength(1)
    expect(within(dialog).getAllByRole('button', { name: 'Cancel' })).toHaveLength(1)
    expect(within(dialog).getByTestId('hub-add-details')).toHaveTextContent('Details')
    expect(text().split('cannot be taken back')).toHaveLength(2) // the caveat stays visible, once
    expect(within(dialog).getByTestId('hub-add-caveat-visible')).toHaveTextContent(/^What .+ has already seen cannot be taken back\.$/)
    // P09: the field list is said once; the hub is named only behind Details
    expect(text().match(/deadline/g)).toHaveLength(1)
    expect(text()).not.toMatch(/\bhub\b/i)
    expect(within(dialog).queryByTestId('hub-add-scope')).toBeNull()
    expect(text()).not.toContain('Comments and files stay private')
    expect(within(dialog).queryByTestId('hub-add-new')).toBeNull()
    expect(within(dialog).queryByTestId('hub-add-leaves')).toBeNull()
    await view.user.click(within(dialog).getByTestId('hub-add-details'))
    expect(await screen.findByTestId('hub-add-new')).toBeTruthy()
    expect(screen.getByTestId('hub-add-leaves')).toHaveTextContent('but never its plan, comments or files')
    expect(screen.getByTestId('hub-add-leaves')).toBeTruthy()
  })

  it('says "Nobody new will see it." when the preview adds nothing and names no readers (P05 review)', async () => {
    const real = api.getHubPreview.bind(api)
    vi.spyOn(api, 'getHubPreview').mockImplementation(async (ws, change) => {
      const p = await real(ws, change)
      return p.review ? { ...p, review: { ...p.review, new_count: 0, new_items: [], leaves: [], widened: [], readers: { ...p.review.readers, people: [], teams: [] } } } : p
    })
    const view = await openMenu('selective')
    await view.user.click(await screen.findByRole('menuitem', { name: 'Share…' }, T))
    await view.user.click(await screen.findByRole('button', { name: 'Share with a colleague' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Share this ticket' }, T)
    await waitFor(() => expect(within(dialog).getByTestId('hub-add-sentence')).toHaveTextContent('Nobody new will see it.'), T)
    expect(dialog.textContent).not.toContain('Only you')
  })
})

it('P17: the badge names the audience when the share data supplies it', async () => {
  vi.spyOn(api, 'getHubShare').mockResolvedValue({ available: true, rules: [{ kind: 'ticket', value: 'DEMO-0043', count: 1, describes: null, audience: { users: ['demo-ben'], groups: [] } }], shared: 1, refused: [], tickets: ['DEMO-0043'], tickets_complete: true })
  vi.spyOn(api, 'getHubAudience').mockResolvedValue({ people: [{ user_id: 'demo-ben', name: 'Ben' }], teams: [], note: '' })
  await openMenu('selective', { ticket: 'DEMO-0043' })
  await waitFor(() => expect(screen.getByTestId('shared-chip')).toHaveTextContent('Shared with Ben'), T)
  expect(screen.getAllByRole('menuitem', { name: 'Share…' })).toHaveLength(1)
  expect(screen.queryByRole('menuitem', { name: 'Share with the hub' })).toBeNull()
})
