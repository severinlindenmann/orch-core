// V3 A7: the hub pages. Mission Control explains and builds the terminal command; it never signs or changes the hub.
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import * as sealed from '@/api/sealed'
import { setHubScenario, setMockConnectError, setMockOneClick, setMockVault, type HubScenario } from '@/mocks/hub'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
beforeEach(() => {
  setHubScenario('standalone')
  setMockOneClick(true)
  setMockConnectError(null)
})
afterEach(() => {
  cleanup()
  setHubScenario(null)
})
const WORDS = 'orchid anchor ribbon velvet quarter timber lantern cobalt meadow harbor window thunder'
// Typing a long JSON string key by key is slow; a paste is one change.
const paste = async (id: string, text: string) => fireEvent.change(await screen.findByTestId(id, {}, T), { target: { value: text } })

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
    expect(screen.getByTestId('hub-full-switch')).toBeTruthy() // one click, also with One click on
  })

  it('full: says the hub reads everything and labels the hub numbers "from the hub, not signed"', async () => {
    hubPage('full')
    expect(await screen.findByText('The hub can read everything in this workspace', {}, T)).toBeTruthy()
    expect(screen.getByText('from the hub, not signed')).toBeTruthy()
    expect(screen.getByText('8 people')).toBeTruthy()
    expect(screen.queryByTestId('hub-full-switch')).toBeNull() // already full: the only way back is leave and join again
    expect(screen.getByText(/Leave this hub, then rejoin/)).toBeTruthy()
  })

  it('left but not told: offers only Stop telling (no retry command yet), in amber', async () => {
    hubPage('nottold')
    expect(await screen.findByText('You left the hub, but the hub does not know yet', {}, T)).toBeTruthy()
    expect(screen.queryByTestId('hub-not-told-try')).toBeNull() // no retry command in core yet
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

describe('joining (A3.1): the person decides, the terminal signs', () => {
  it('pre-selects nothing, then hands over the exact command', async () => {
    setMockOneClick(false) // a full join is possible only with One click off and the key under a passphrase
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await view.user.click(await screen.findByTestId('hub-join-terminal', {}, T))
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

describe('switching to full access (run-9 feedback 6): one click, no backup key', () => {
  beforeEach(() => setMockVault(true))
  afterEach(() => setMockVault(false))
  it('is one click with One click on: review, sign, the hub is told, then full', async () => {
    const connectHub = vi.spyOn(api, 'connectHub')
    const prepare = vi.spyOn(api, 'prepareClick')
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-full-switch', {}, T))
    expect(screen.getByText(/One way/)).toBeTruthy()
    const btn = await screen.findByRole('button', { name: 'Sign and let the hub read everything' }, T)
    expect(screen.queryByTestId('hub-backup-input')).toBeNull() // the person key is kept: nothing to type
    await waitFor(() => expect(btn).toBeEnabled(), { timeout: 5000 })
    fireEvent.pointerDown(btn)
    fireEvent.click(btn)
    await waitFor(() => expect(connectHub).toHaveBeenCalledTimes(1), T)
    await waitFor(() => expect(screen.queryByTestId('hub-full-stage')).toBeNull(), T) // the dialog closed
    expect(prepare).toHaveBeenCalledTimes(1) // only the switch itself: nothing is prepared after the connect
    expect(connectHub.mock.calls[0][1].invite).toBeUndefined() // a mode switch has no invite
  })
})

describe('leaving (A3.5 / A3.6)', () => {
  it('normal leave lists the consequences before the command, in a terminal if you ask for it', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-leave', {}, T))
    expect(screen.getByText(/cannot be taken back/)).toBeTruthy()
    await view.user.click(await screen.findByTestId('hub-leave-terminal', {}, T))
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub leave')
  })

  it('the way out without the hub is offered only from the offline banner', async () => {
    const view = hubPage('offline', '/w/DEMO')
    const banner = await screen.findByTestId('hub-offline-banner', {}, T)
    expect(within(banner).getByText('Hub offline, working locally')).toBeTruthy()
    await view.user.click(within(banner).getByTestId('hub-leave-down'))
    expect(await screen.findByText('Leave without the hub', {}, T)).toBeTruthy()
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub leave')
    expect(screen.queryByTestId('hub-backup-input')).toBeNull() // the hub cannot be reached: a terminal only
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
    expect(screen.getByTestId('ticket-hub-DEMO-0042').textContent).toBe('Only here')
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

  it('shows it in the review and hands the invite to the terminal unchanged', async () => {
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await view.user.click(await screen.findByTestId('hub-join-terminal', {}, T))
    await type(view, 'hub-invite', '9f3c1ab277de40c8aa11bb22cc33dd44:7QK2M9XD')
    await type(view, 'hub-card', 'card.json')
    await view.user.click(screen.getByTestId('hub-join-continue'))
    await view.user.click(screen.getByTestId('hub-mode-selective'))
    await view.user.click(screen.getByTestId('hub-join-review'))
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub join 9f3c1ab277de40c8aa11bb22cc33dd44:7QK2M9XD --hub-card card.json --mode selective')
    expect(screen.getByTestId('hub-invite-id').textContent).toContain('9f3c 1ab2 77de 40c8')
  })
})

describe('by click (#1198 C11)', () => {
  // The mock workspace id is not a 32-hex id, which the real seal requires; HubStep.test.tsx covers the real seal.
  let seal: ReturnType<typeof vi.spyOn>
  beforeEach(() => { seal = vi.spyOn(sealed, 'seal').mockResolvedValue({ v: 1, alg: 'P256-HKDF-SHA256-A256GCM', epk: 'e', ct: 'c' } as sealed.Envelope) })
  afterEach(() => vi.restoreAllMocks())
  const sign = async (label: string) => {
    fireEvent.change(await screen.findByTestId('hub-backup-input', {}, T), { target: { value: WORDS } })
    const btn = await screen.findByRole('button', { name: label }, T)
    await waitFor(() => expect(btn).toBeEnabled(), { timeout: 5000 })
    fireEvent.pointerDown(btn)
    fireEvent.click(btn)
  }

  const LINK = 'http://10.0.0.5:8106/c#h1.9f3c1ab277de40c8aa11bb22cc33dd44.7QK2M9XD.10.0.0.5:8105'
  const pasteLink = async (link = LINK) => paste('hub-link-text', link)

  it('connects from a link: the hub is shown at once, one click with the backup key, then connected', async () => {
    const connectHub = vi.spyOn(api, 'connectHub')
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await pasteLink('not a link')
    expect(screen.getByTestId('hub-link-bad')).toBeTruthy()
    await pasteLink()
    expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('10.0.0.5:8106')
    expect(screen.getByTestId('hub-link-hub').textContent).toContain('9f3c 1ab2 77de 40c8')
    expect(screen.getByTestId('hub-link-hub').textContent).toContain('Only what I choose')
    expect(screen.queryByTestId('hub-card-text')).toBeNull() // no card file, no separate invite, no mode step
    await sign('Sign and join the hub')
    expect(await screen.findByTestId('hub-connected', {}, T)).toHaveTextContent(/Connected/)
    expect(seal).toHaveBeenCalledTimes(1)
    expect(seal.mock.calls[0][1]).toBe(WORDS)
    expect(connectHub).toHaveBeenCalledTimes(1) // the host tells the hub right after the signed join, with the link's code
    expect(connectHub.mock.calls[0][1]).toMatchObject({ invite: '7QK2M9XD', addr: '10.0.0.5:8105', idem: expect.stringMatching(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/) })
    expect(screen.queryByTestId('hub-command')).toBeNull() // no terminal command anywhere in this path
    await view.user.click(screen.getByTestId('hub-connect-close'))
    expect(await screen.findByText('Connected · Hub sees only what you share', {}, T)).toBeTruthy()
  })

  it('with the person key kept the join is one click and asks for no backup key, also in full mode', async () => {
    setMockVault(true)
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await pasteLink()
    await view.user.click(await screen.findByTestId('hub-mode-full', {}, T))
    const btn = await screen.findByRole('button', { name: 'Sign and join the hub' }, T)
    expect(screen.queryByTestId('hub-backup-input')).toBeNull()
    await waitFor(() => expect(btn).toBeEnabled(), { timeout: 5000 })
    fireEvent.pointerDown(btn)
    fireEvent.click(btn)
    expect(await screen.findByTestId('hub-connected', {}, T)).toHaveTextContent(/read everything/)
    setMockVault(false)
  })

  it('with One click off, connecting says why and offers the terminal, not a dead button', async () => {
    setMockOneClick(false)
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await pasteLink()
    expect(await screen.findByTestId('hub-no-one-click', {}, T)).toHaveTextContent(/One click is off/)
    expect(screen.queryByTestId('hub-backup-input')).toBeNull()
    await view.user.click(screen.getByTestId('hub-join-terminal-instead'))
    expect(await screen.findByTestId('hub-invite', {}, T)).toBeTruthy()
  })

  it('a join the hub refuses is not "Connected": it says so, keeps the way out and can be tried again', async () => {
    setMockConnectError('The invite was not accepted.')
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await pasteLink()
    await sign('Sign and join the hub')
    const failed = await screen.findByTestId('hub-connect-failed', {}, T)
    expect(failed.textContent).toMatch(/not connected yet/)
    expect(failed.textContent).toMatch(/The invite was not accepted\./)
    expect(failed.textContent).toMatch(/leave the hub and join again/)
    expect(screen.queryByText(/^Connected\./)).toBeNull()
    setMockConnectError(null)
    await view.user.click(screen.getByTestId('hub-connect-retry'))
    expect(await screen.findByTestId('hub-connected', {}, T)).toHaveTextContent(/Connected/)
  })

  it('leaves: the consequences, then one click with the backup key', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-leave', {}, T))
    expect(screen.getByText(/cannot be taken back/)).toBeTruthy()
    expect(screen.queryByTestId('hub-command')).toBeNull()
    await sign('Sign and leave the hub')
    expect(await screen.findByTestId('hub-step-done', {}, T)).toHaveTextContent(/You left the hub/)
    await view.user.click(within(screen.getByTestId('hub-step')).getByRole('button', { name: 'Close' }))
    expect(await screen.findByText(/Not connected\./, {}, T)).toBeTruthy()
  })

  it('with One click off, leaving says why and offers the terminal', async () => {
    setMockOneClick(false)
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-leave', {}, T))
    expect(await screen.findByTestId('hub-no-one-click', {}, T)).toHaveTextContent(/One click is off/)
    await view.user.click(screen.getByTestId('hub-leave-terminal'))
    expect((await screen.findByTestId('hub-command', {}, T)).textContent).toBe('orch hub leave')
  })
})
