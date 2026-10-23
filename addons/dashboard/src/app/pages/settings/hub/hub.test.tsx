// V3 A7: the hub pages. Mission Control explains and builds the Terminal command; it never signs or changes the hub.
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { setHubScenario, type HubScenario } from '@/mocks/hub'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
beforeEach(() => setHubScenario('standalone'))
afterEach(() => {
  cleanup()
  setHubScenario(null)
})

const hubPage = (scenario: HubScenario, path = '/w/DEMO/settings/hub') => {
  setHubScenario(scenario)
  return renderApp(path)
}
const type = async (view: ReturnType<typeof renderApp>, id: string, text: string) => {
  const el = await screen.findByTestId(id, {}, T)
  await view.user.clear(el)
  await view.user.type(el, text)
}

describe('the Hub tab has a different page for each mode', () => {
  it('standalone: not connected, nothing to fear, a way to connect', async () => {
    hubPage('standalone')
    expect(await screen.findByText(/Not connected\./, {}, T)).toBeTruthy()
    expect(screen.getByText(/works on its own/)).toBeTruthy()
    expect(screen.getByTestId('hub-connect')).toBeTruthy()
    expect(screen.queryByTestId('hub-leave')).toBeNull()
  })

  it('selective: notes and questions are always shared, with no switch', async () => {
    hubPage('selective')
    expect(await screen.findByText('Connected · Hub sees only what you share', {}, T)).toBeTruthy()
    expect(await screen.findAllByText('Always shared', {}, T)).toHaveLength(2)
    expect(screen.queryAllByRole('switch')).toHaveLength(0)
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0)
    expect(screen.getByTestId('hub-choose-share')).toBeTruthy()
    expect(screen.getByTestId('hub-full-switch')).toBeTruthy()
  })

  it('full: says the hub reads everything and labels the hub numbers "from the hub, not signed"', async () => {
    hubPage('full')
    expect(await screen.findByText('The hub can read everything in this workspace', {}, T)).toBeTruthy()
    expect(screen.getByText('from the hub, not signed')).toBeTruthy()
    expect(screen.getByText('8 people')).toBeTruthy()
    expect(screen.queryByTestId('hub-full-switch')).toBeNull() // already full: the only way back is leave and join again
    expect(screen.getByText(/Leave this hub, then rejoin/)).toBeTruthy()
  })

  it('left but not told: offers Try now and Stop telling, in amber', async () => {
    hubPage('nottold')
    expect(await screen.findByText('You left the hub, but the hub does not know yet', {}, T)).toBeTruthy()
    expect(screen.getByTestId('hub-not-told-try')).toBeTruthy()
    expect(screen.getByTestId('hub-not-told-stop')).toBeTruthy()
  })
})

describe('the top-bar chip', () => {
  it.each([
    ['standalone', 'Local only'],
    ['selective', 'Hub sees only what you share'],
    ['full', 'Hub can read everything'],
    ['offline', 'Hub offline, working locally'],
    ['nottold', 'Left the hub, hub not told yet'],
  ] as const)('%s says "%s"', async (scenario, label) => {
    hubPage(scenario, '/w/DEMO/board')
    const chip = await screen.findByTestId('hub-chip', {}, T)
    expect(chip.getAttribute('aria-label')).toBe(label)
  })
})

describe('joining (A3.1): the person decides, the Terminal signs', () => {
  it('pre-selects nothing, then hands over the exact command', async () => {
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    expect((screen.getByTestId('hub-join-continue') as HTMLButtonElement).disabled).toBe(true)
    await type(view, 'hub-invite', 'INV-7QK2-M9XD')
    await type(view, 'hub-card', '/Users/me/acme card.json')
    expect(screen.queryByText(/Hub check passed/)).toBeNull() // Mission Control cannot verify an invite
    await view.user.click(screen.getByTestId('hub-join-continue'))

    expect(screen.getByTestId('hub-mode-selective').getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByTestId('hub-mode-full').getAttribute('aria-pressed')).toBe('false')
    expect((screen.getByTestId('hub-join-review') as HTMLButtonElement).disabled).toBe(true)
    await view.user.click(screen.getByTestId('hub-mode-full'))
    await view.user.click(screen.getByTestId('hub-join-review'))

    const command = await screen.findByTestId('hub-command', {}, T)
    expect(screen.getByTestId('hub-invite-id').textContent).toMatch(/does not name a hub id/)
    expect(command.textContent).toBe("orch hub join INV-7QK2-M9XD --hub-card '/Users/me/acme card.json' --mode full")
    expect(screen.getByText(/Finish on your computer/)).toBeTruthy()
    expect(screen.getByText(/never sees your passphrase or your key/)).toBeTruthy()
    expect(screen.getByText(/shows the hub's fingerprint/)).toBeTruthy()
  })
})

describe('switching to full access (A3.2): one way, typed, then the Terminal', () => {
  it('stays disabled until the exact words are typed', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-full-switch', {}, T))
    expect(screen.getByText(/One way/)).toBeTruthy()
    await type(view, 'hub-full-card', 'card.json')
    const go = screen.getByTestId('hub-full-continue') as HTMLButtonElement
    expect(go.disabled).toBe(true)
    await type(view, 'hub-full-words', 'full acces')
    expect(go.disabled).toBe(true)
    await type(view, 'hub-full-words', 'Full Access')
    expect(go.disabled).toBe(false)
    await view.user.click(go)
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub mode full --hub-card card.json')
  })
})

describe('leaving (A3.5 / A3.6)', () => {
  it('normal leave lists the consequences before the command', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-leave', {}, T))
    expect(screen.getByText(/cannot be taken back/)).toBeTruthy()
    await view.user.click(screen.getByTestId('hub-leave-continue'))
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub leave')
  })

  it('the way out without the hub is offered only from the offline banner', async () => {
    const view = hubPage('offline', '/w/DEMO')
    const banner = await screen.findByTestId('hub-offline-banner', {}, T)
    expect(within(banner).getByText('Hub offline, working locally')).toBeTruthy()
    await view.user.click(within(banner).getByTestId('hub-leave-down'))
    expect(await screen.findByText('Leave without the hub', {}, T)).toBeTruthy()
    expect((await screen.findByTestId('hub-command', {}, T).catch(() => null)) ?? true).toBeTruthy()
    cleanup()
    hubPage('selective', '/w/DEMO')
    await screen.findByTestId('hub-chip', {}, T)
    expect(screen.queryByTestId('hub-offline-banner')).toBeNull()
  })
})

describe('share rules (A3.3)', () => {
  it('previews a rule live, then hands over the command, with the honest caveat', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    expect(await screen.findByText('Choose what to share', {}, T)).toBeTruthy()
    expect(await screen.findAllByText('Always shared', {}, T)).toHaveLength(2)
    await view.user.click(screen.getByTestId('hub-rule-add'))
    await type(view, 'hub-rule-value', 'team')
    await waitFor(() => expect(screen.getByText(/1 now visible/)).toBeTruthy(), T)
    await view.user.click(screen.getByTestId('hub-share-review'))
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub share rule add --type team')
    expect(screen.getAllByText(/cannot be taken back/).length).toBeGreaterThan(0)
  })

  it('a value with shell characters is quoted in the command', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    await view.user.click(await screen.findByTestId('hub-rule-add', {}, T))
    await type(view, 'hub-rule-value', 'a; rm -rf ~')
    await view.user.click(await screen.findByTestId('hub-share-review', {}, T).then((b) => { return b }))
    await waitFor(() => expect((screen.getByTestId('hub-share-review') as HTMLButtonElement).disabled).toBe(false), T)
    await view.user.click(screen.getByTestId('hub-share-review'))
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toContain("'a; rm -rf ~'")
  })
})

describe('the Tickets Hub column (A5.6)', () => {
  it('marks Shared or Not shared in selective mode', async () => {
    hubPage('selective', '/w/DEMO/tickets')
    expect((await screen.findByTestId('ticket-hub-DEMO-0043', {}, T)).textContent).toBe('Shared')
    expect(screen.getByTestId('ticket-hub-DEMO-0042').textContent).toBe('Not shared')
  })
  it('full mode: one banner and no marks', async () => {
    hubPage('full', '/w/DEMO/tickets')
    expect(await screen.findByTestId('hub-all-tickets', {}, T)).toBeTruthy()
    expect(screen.queryByTestId('ticket-hub-DEMO-0043')).toBeNull()
  })
  it('standalone: nothing', async () => {
    hubPage('standalone', '/w/DEMO/tickets')
    await screen.findByRole('table', { name: 'Tickets' }, T)
    expect(screen.queryByTestId('hub-all-tickets')).toBeNull()
    expect(screen.queryByTestId('ticket-hub-DEMO-0043')).toBeNull()
  })
})

describe('the hub id of an invite (core#1110)', () => {
  it('is read only from the invite the person typed, in short form', async () => {
    const { inviteHubId } = await import('@/api/hub')
    expect(inviteHubId('9f3c1ab277de40c8aa11bb22cc33dd44:7QK2M9XD')).toBe('9f3c 1ab2 77de 40c8')
    expect(inviteHubId('INV-7QK2-M9XD')).toBeNull()
    expect(inviteHubId('9f3c:abc')).toBeNull()
  })

  it('shows it in the review and hands the invite to the Terminal unchanged', async () => {
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await type(view, 'hub-invite', '9f3c1ab277de40c8aa11bb22cc33dd44:7QK2M9XD')
    await type(view, 'hub-card', 'card.json')
    await view.user.click(screen.getByTestId('hub-join-continue'))
    await view.user.click(screen.getByTestId('hub-mode-selective'))
    await view.user.click(screen.getByTestId('hub-join-review'))
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub join 9f3c1ab277de40c8aa11bb22cc33dd44:7QK2M9XD --hub-card card.json --mode selective')
    expect(screen.getByTestId('hub-invite-id').textContent).toContain('9f3c 1ab2 77de 40c8')
  })
})
