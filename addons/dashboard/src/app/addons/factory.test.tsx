import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { seedRuns } from '@/mocks/addons/factory-runs'
import { installAndGrant } from '@/test/installAddon'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const on = (s: MockStore) => installAndGrant(s, wsOf(s), 'factory')

describe('AI Factory page', () => {
  it('shows the epic, its limits, progress and the children, with a Preview chip next to the title and the nav entry', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    const h1 = await screen.findByRole('heading', { level: 1, name: /AI Factory/ }, T)
    expect(within(h1).getByText('Preview')).toBeInTheDocument()
    expect(await screen.findByText(/Monthly billing v2/, {}, T)).toBeInTheDocument()
    expect(screen.getByText(/25 children or 72 hours/)).toBeInTheDocument()
    expect(screen.getAllByRole('progressbar').length).toBeGreaterThanOrEqual(2)
    for (const name of ['Overview', /^Children/, /^Permits/]) expect(screen.getByRole('tab', { name })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Approval' })).not.toBeInTheDocument() // the Children tab is not open
    await user.click(screen.getByRole('tab', { name: /^Children/ }))
    expect(await screen.findByRole('columnheader', { name: 'Approval' })).toBeInTheDocument()
    expect(screen.getAllByText('auto-approved by agent', { selector: 'td' }).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    await user.click(await screen.findByRole('button', { name: /More addons/ }))
    const nav = await screen.findByRole('link', { name: /^AI Factory/ })
    expect(within(nav).getByText('Preview')).toBeInTheDocument()
  })
  it('the addon manager row carries the Preview chip too', async () => {
    renderApp('/settings/addons', { viewer: 'p_sev', setup: on })
    const row = await screen.findByRole('row', { name: /AI Factory 0\.1\.0/ }, T)
    expect(within(row).getByText('Preview')).toBeInTheDocument()
  })
  it('Pause factory is signed in core\'s dialog; the paused state is calm (info), and Resume brings it back', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('button', { name: 'Pause factory' }, T))
    const dialog = await screen.findByRole('dialog', { name: /Sign: Pause \(pause\) · AI Factory \(factory\)/ }, T)
    expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('running')
    await user.click(within(dialog).getByRole('button', { name: /Sign and run/ }))
    const alert = (await screen.findByText(/^Paused by/, {}, T)).closest('[role="status"]')!
    expect(alert.className).toMatch(/info/)
    expect(alert.className).not.toMatch(/warning|danger/)
    await waitFor(() => expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('paused'), T)
    await user.click(await screen.findByRole('button', { name: 'Resume' }, T))
    await user.click(within(await screen.findByRole('dialog', { name: /Sign: Resume \(resume\) · AI Factory \(factory\)/ }, T)).getByRole('button', { name: /Sign and run/ }))
    await waitFor(() => expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('running'), T)
  })
  it('Run demo activity shows a persistent info alert, and Stop demo activity ends it', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('button', { name: 'Run demo activity' }, T))
    expect(await screen.findByText(/Demo activity is running: a new child and permit about every 20 s, at most 10 an hour\./, {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop demo activity' }))
    await waitFor(() => expect(screen.queryByText(/Demo activity is running/)).not.toBeInTheDocument(), T)
  })
  it('warns at 80% of the child budget', async () => {
    renderApp('/addon/factory/factory', {
      viewer: 'p_sev',
      setup: (s) => {
        on(s)
        ;(s.addonState(wsOf(s), 'factory') as { used: number }).used = 22
      },
    })
    expect(await screen.findByText('22 of 25 children used. The factory stops at 25.', {}, T)).toBeInTheDocument()
  })
  it('an open permit is answered in place, with the same signing prompt as Today', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    expect(screen.queryByText(/Answer on Today/)).not.toBeInTheDocument()
    // What waits for a person is above the tabs: no tab click needed, and it says how many.
    expect(await screen.findByText('1 permission request needs your decision', {}, T)).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /^Overview/ })).toHaveAttribute('aria-selected', 'true')
    await user.click(await screen.findByRole('button', { name: 'Grant once' }, T))
    await user.click(await screen.findByRole('button', { name: 'Send answer' }, T))
    await waitFor(() => expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.permit_granted')).toBe(true), T)
  })
  it('cancelling the signing dialog changes nothing', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('button', { name: 'Pause factory' }, T))
    const dialog = await screen.findByRole('dialog', { name: /Sign: Pause \(pause\) · AI Factory \(factory\)/ }, T)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), T)
    expect(mockStore.addonStateView(wsOf(mockStore), 'factory')!.mode).toBe('running')
    expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.paused')).toBe(false)
  })
  it('a viewer sees Pause and Run demo activity disabled', async () => {
    renderApp('/addon/factory/factory', { viewer: 'p_tom', setup: on })
    const pause = await screen.findByRole('button', { name: 'Pause factory' }, T)
    await waitFor(() => expect(pause).toBeDisabled(), T)
    expect(screen.getByRole('button', { name: 'Run demo activity' })).toBeDisabled()
  })
})

describe('permits on the page', () => {
  it('the Permits tab keeps the history and the open request is not drawn twice', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    await screen.findByRole('button', { name: 'Grant once' }, T)
    await user.click(await screen.findByRole('tab', { name: /^Permits\s*\d+/ }, T))
    expect(await screen.findByRole('columnheader', { name: 'Command' }, T)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Grant once' })).toHaveLength(1)
  })
  it('a viewer sees no decision above the tabs (they cannot answer)', async () => {
    renderApp('/addon/factory/factory', { viewer: 'p_tom', setup: on })
    await screen.findByRole('tab', { name: /^Overview/ }, T)
    expect(screen.queryByText(/needs your decision|need your decision/)).not.toBeInTheDocument()
  })
})

describe('permits on Today', () => {
  it('a permit with a 600-character command shows the whole command in core\'s prompt (never cut)', async () => {
    const LONG = `bash -c '${'echo safe; '.repeat(55)}rm -rf ~/TAIL'`
    expect(LONG.length).toBeGreaterThan(600)
    const { user } = renderApp('/', {
      viewer: 'p_sev',
      setup: (s) => {
        on(s)
        for (const p of s.addonState(wsOf(s), 'factory').permits as { command: string }[]) p.command = LONG
      },
    })
    const card = (await screen.findAllByTestId(/^card-addon:factory\.permit:/, {}, T))[0]
    await user.click(within(card).getByRole('button', { name: 'Decide' }))
    await user.click(within(card).getByRole('button', { name: 'Grant once' }))
    const dialog = await screen.findByRole('dialog', {}, T)
    const region = within(dialog).getByRole('region', { name: 'From addon factory' })
    expect(region.textContent).toContain(LONG)
    expect(region.textContent).toContain('rm -rf ~/TAIL')
  })
  it('Grant once removes the card and logs factory.permit_granted on the epic', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: on })
    const card = (await screen.findAllByTestId(/^card-addon:factory\.permit:/, {}, T))[0]
    const id = card.getAttribute('data-testid')!.replace('card-addon:', '')
    await user.click(within(card).getByRole('button', { name: 'Decide' }))
    expect(within(card).getByRole('button', { name: 'Grant for this epic' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Refuse' })).toBeInTheDocument()
    await user.click(within(card).getByRole('button', { name: 'Grant once' }))
    const dialog = await screen.findByRole('dialog', {}, T)
    // The question (with the command it quotes) is the addon's text: shown in full in the labelled region.
    expect(within(dialog).getByRole('region', { name: 'From addon factory' }).textContent).toMatch(/Question: .* asks to run: \S+/)
    await user.click(within(dialog).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(screen.queryByTestId(`card-addon:${id}`)).not.toBeInTheDocument(), T)
    expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.permit_granted' && e.scope === 'once')).toBe(true)
  })
})

// Full runs (owner decision 2026-10-10 evening, D61 option).
describe('factory full runs', () => {
  const holding = (s: MockStore) => {
    on(s)
    Object.assign(s.addonState(wsOf(s), 'factory'), seedRuns(s, 'p_sev'))
  }
  it('the request form: Deliver needs a destination; the signing prompt names every value in core lines', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: on })
    await user.click(await screen.findByRole('tab', { name: /^Full runs/ }, T))
    await user.type(await screen.findByLabelText(/^Goal/, {}, T), 'Autumn tariff campaign')
    await user.click(screen.getByRole('radio', { name: /^All the way to Deliver/ }))
    await user.click(screen.getByRole('button', { name: 'Review request' }))
    // No destination: nothing is reviewed, the field says why.
    expect((await screen.findAllByText(/Fill in What Deliver means/, {}, T)).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Sign and start' })).not.toBeInTheDocument()
    await user.type(screen.getByLabelText(/^What Deliver means/), 'Publish campaign')
    await user.selectOptions(screen.getByLabelText(/^Hold window/), '1 h')
    await user.click(screen.getByRole('button', { name: 'Review request' }))
    await user.click(await screen.findByRole('button', { name: 'Sign and start' }, T))
    const dialog = await screen.findByRole('dialog', { name: /^Sign: Start run \(start_run\) · AI Factory \(factory\)$/ }, T)
    const lines = [...dialog.querySelectorAll('[data-arg-key]')].map((e) => [e.getAttribute('data-arg-key'), e.getAttribute('data-arg-value')])
    expect(lines).toEqual([['goal', 'Autumn tariff campaign'], ['goes_up_to', 'Deliver'], ['deliver_means', 'Publish campaign'], ['hold_minutes', '60'], ['largest_child', 'm']])
    expect(dialog.textContent).toContain('Deliver means (deliver_means): Publish campaign')
    await user.click(within(dialog).getByRole('button', { name: 'Sign and run' }))
    await waitFor(() => expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.run_requested' && e.deliver_means === 'Publish campaign')).toBe(true), T)
    expect(await screen.findByText('R-1 · Autumn tariff campaign', {}, T)).toBeInTheDocument()
  })
  it('a run on hold: the calm notice above the tabs, on Today and in the shell; Stop from the shell cancels it', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: holding })
    expect(await screen.findByText(/Delivering in 28 min · Publish campaign to the newsletter list/, { selector: 'strong' }, T)).toBeInTheDocument()
    const banner = await screen.findByTestId('delivery-hold-banner', {}, T)
    expect(banner).toHaveTextContent(/^Delivering in 28 min · Publish campaign to the newsletter list · at \d\d:\d\d · AI Factory \(factory\)/)
    expect(banner.className).not.toMatch(/warning|danger|orange/)
    await user.click(within(banner).getByRole('button', { name: 'Stop…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Decide for AI Factory (factory)' }, T)
    expect(dialog.textContent).toContain('Deliver means (deliver_means): Publish campaign to the newsletter list')
    await user.click(within(dialog).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(screen.queryByTestId('delivery-hold-banner')).not.toBeInTheDocument(), T)
    expect(mockStore.eventsOf('DEMO-0050').filter((e) => e.type === 'factory.deliver_stopped')).toHaveLength(1)
    expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.delivered' && e.run === 'R-2')).toBe(false)
  })
  it('Today lists the hold as a needs-you item with Stop delivery', async () => {
    renderApp('/', { viewer: 'p_sev', setup: holding })
    const card = await screen.findByTestId('card-addon:factory.hold:R-2', {}, T)
    expect(card).toHaveTextContent(/Delivering in 28 min: Publish campaign to the newsletter list/)
  })
  it('Skip the wait (demo) delivers: "Delivered: <destination> at <time>"', async () => {
    const { user } = renderApp('/addon/factory/factory', { viewer: 'p_sev', setup: holding })
    await user.click(await screen.findByRole('button', { name: 'Skip the wait (demo)' }, T))
    await waitFor(() => expect(screen.queryByTestId('delivery-hold-banner')).not.toBeInTheDocument(), T)
    await user.click(await screen.findByRole('tab', { name: /^Full runs/ }, T))
    expect(await screen.findByText(/^Delivered: Publish campaign to the newsletter list at /, {}, T)).toBeInTheDocument()
    expect(mockStore.eventsOf('DEMO-0050').some((e) => e.type === 'factory.delivered' && e.run === 'R-2')).toBe(true)
  })
})
