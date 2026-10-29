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
  it('offers "Share with the hub" to the owner in share-some mode', async () => {
    await openMenu('selective')
    expect(await screen.findByRole('menuitem', { name: 'Share with the hub' }, T)).toBeTruthy()
  })

  it.each(['standalone', 'full'] as const)('offers nothing in %s mode', async scenario => {
    await openMenu(scenario)
    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByRole('menuitem', { name: /Share with the hub|Stop sharing/ })).toBeNull()
  })

  it('offers nothing when the workspace has not joined a hub', async () => {
    await openMenu('nottold')
    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByRole('menuitem', { name: /Share with the hub|Stop sharing/ })).toBeNull()
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
    expect(screen.queryByRole('menuitem', { name: /Share with the hub|Stop sharing/ })).toBeNull()
  })

  it('offers nothing to a viewer who is not the owner', async () => {
    await openMenu('selective', { viewer: 'p_tom' })
    await new Promise(r => setTimeout(r, 300))
    expect(screen.queryByRole('menuitem', { name: /Share with the hub|Stop sharing/ })).toBeNull()
  })

  it('shows the same review as Settings, binds its plan, and shares with one click', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
    const add = vi.spyOn(api, 'addHubRule')
    const ok = vi.spyOn(toast, 'success')
    const preview = vi.spyOn(api, 'getHubPreview')
    const view = await openMenu('selective')
    await view.user.click(await screen.findByRole('menuitem', { name: 'Share with the hub' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Share this ticket with the hub' }, T)
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    const go = await within(dialog).findByTestId('hub-add-go', {}, T)
    await waitFor(() => expect(go).toBeEnabled(), T)
    expect(within(dialog).getByTestId('hub-add-readers')).toBeTruthy() // who will see it
    expect(within(dialog).getByTestId('hub-add-sentence')).toHaveTextContent('ticket DEMO-0042')
    const shownPlan = await preview.mock.results[preview.mock.results.length - 1].value.then((p: { plan: string }) => p.plan)
    expect(preview).toHaveBeenLastCalledWith(expect.any(String), { add: { kind: 'ticket', value: 'DEMO-0042', description: true } })
    fireEvent.click(go)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Share this ticket with the hub' })).toBeNull(), T)
    expect(ok).toHaveBeenCalledWith(expect.stringMatching(/^Shared “.+” with the hub\.$|^Shared “.+” with .+\.$/), expect.anything())
    expect(add).toHaveBeenCalledTimes(1)
    expect(add.mock.calls[0][1]).toEqual({ kind: 'ticket', value: 'DEMO-0042', description: true })
    expect(add.mock.calls[0][2]).toBe(shownPlan)
  })

  it('an untrusted click shares nothing', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(false)
    const add = vi.spyOn(api, 'addHubRule')
    const view = await openMenu('selective')
    await view.user.click(await screen.findByRole('menuitem', { name: 'Share with the hub' }, T))
    const go = await screen.findByTestId('hub-add-go', {}, T)
    await waitFor(() => expect(go).toBeEnabled(), T)
    fireEvent.click(go)
    expect(add).not.toHaveBeenCalled()
  })

  it('shows a Shared chip on the ticket once the hub has it (r10-P08)', async () => {
    await openMenu('selective', { ticket: 'DEMO-0043' })
    expect(await screen.findByTestId('shared-chip', {}, T)).toHaveTextContent('Shared with the hub')
  })

  it('says "Stop sharing" when the hub already has it, and opens the stop review', async () => {
    const view = await openMenu('selective', { ticket: 'DEMO-0043' }) // the mock hub already has DEMO-0043
    await view.user.click(await screen.findByRole('menuitem', { name: 'Stop sharing' }, T))
    expect(await screen.findByRole('dialog', { name: 'Stop sharing this ticket' }, T)).toBeTruthy()
  })
})
