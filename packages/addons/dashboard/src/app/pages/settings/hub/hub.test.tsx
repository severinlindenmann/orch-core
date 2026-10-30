// V3 A7: the hub pages. Mission Control explains and builds the terminal command; it never signs or changes the hub.
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import * as sealed from '@/api/sealed'
import * as gesture from '@/components/sign/trustedPress'
import { setHubScenario, setMockConnectError, setMockOneClick, setMockVault, type HubScenario } from '@/mocks/hub'
import { trustedPresses } from '@/test/hostHarness'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
// Compile lazy page modules before measuring UI waits; cold transforms can exceed the
// four-second assertion window on a busy machine.
beforeAll(async () => {
  await Promise.all([import('@/app/pages/settings'), import('@/app/pages/tickets')])
}, 60000)
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

  it('full: says the hub reads everything and keeps "from the hub, not signed" behind Details', async () => {
    hubPage('full')
    expect(await screen.findByText('The hub can read everything in this workspace', {}, T)).toBeTruthy()
    expect(screen.queryByText(/not signed/)).toBeNull()
    fireEvent.click(screen.getByTestId('hub-access-details'))
    expect(screen.getByText('From the hub, not signed.')).toBeTruthy()
    expect(screen.getByText('8 people')).toBeTruthy()
    expect(screen.queryByTestId('hub-full-switch')).toBeNull() // already full: the only way back is leave and join again
    expect(screen.getByText(/Leave this hub, then rejoin/)).toBeTruthy()
  })

  it('uses the hub name when supplied and singular people counts', async () => {
    const original = api.getHubStatus.bind(api)
    const status = vi.spyOn(api, 'getHubStatus').mockImplementation(async ws => ({
      ...await original(ws), hub_name: 'Acme hub', groups: { read: 0, edit: 1, admin: 2 },
    }))
    hubPage('full')
    expect(await screen.findByText('Acme hub', {}, T)).toBeTruthy()
    expect(screen.getByText('0 people')).toBeTruthy()
    expect(screen.getByText('1 person')).toBeTruthy()
    expect(screen.getByText('2 people')).toBeTruthy()
    status.mockRestore()
  })

  it('left but not told: says it keeps trying, offers no terminal step and no retry', async () => {
    hubPage('nottold')
    expect(await screen.findByText('You left the hub, but the hub does not know yet', {}, T)).toBeTruthy()
    expect(screen.getByText('The workspace keeps trying to tell the hub whenever it is reachable.')).toBeTruthy()
    expect(screen.queryByTestId('hub-not-told-try')).toBeNull() // no retry command in core yet
    expect(screen.queryByTestId('hub-not-told-stop')).toBeNull() // stopping was a terminal hand-off (removed)
    expect(screen.queryByTestId('hub-command')).toBeNull()
  })
})

describe('the top-bar chip', () => {
  it.each([
    ['standalone', 'Only on this computer'],
    ['selective', 'Online · connected to hub'],
    ['full', 'Online · connected to hub'],
    ['offline', 'Hub offline, working locally'],
    ['nottold', 'Left the hub, hub not told yet'],
  ] as const)('%s says "%s"', async (scenario, label) => {
    hubPage(scenario, '/w/DEMO/board')
    const chip = await screen.findByTestId('hub-chip', {}, T)
    expect(chip.getAttribute('aria-label')).toBe(label)
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
    expect(screen.getByText(/To share less/)).toBeTruthy()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getAllByRole('heading', { name: 'Let the hub read everything' })).toHaveLength(1)
    expect(within(dialog).getByText('Hub admins can read every ticket, file and note.')).toBeTruthy()
    expect(within(dialog).getByText('Hub admins become admins here.')).toBeTruthy()
    expect(within(dialog).getByText("You'll approve on your phone or passkey.")).toBeTruthy()
    expect(within(dialog).queryByText('Before:')).toBeNull()
    const btn = await screen.findByRole('button', { name: 'Sign and let the hub read everything' }, T)
    expect(screen.queryByTestId('hub-backup-input')).toBeNull() // the person key is kept: nothing to type
    await waitFor(() => expect(btn).toBeEnabled(), { timeout: 5000 })
    trustedPresses() // the armed Sign signs only on a trusted press (fireEvent is untrusted)
    fireEvent.pointerDown(btn)
    fireEvent.click(btn)
    await waitFor(() => expect(connectHub).toHaveBeenCalledTimes(1), T)
    await waitFor(() => expect(screen.queryByTestId('hub-full-stage')).toBeNull(), T) // the dialog closed
    expect(prepare).toHaveBeenCalledTimes(1) // only the switch itself: nothing is prepared after the connect
    expect(connectHub.mock.calls[0][1].invite).toBeUndefined() // a mode switch has no invite
  })
})

describe('leaving (A3.5 / A3.6)', () => {
  it('normal leave has one exit and no terminal offer', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-leave', {}, T))
    expect(screen.getByText(/cannot be taken back/)).toBeTruthy()
    expect(screen.queryByTestId('hub-leave-terminal')).toBeNull()
    expect(screen.queryByTestId('hub-flow-close')).toBeNull()
    expect(await screen.findByRole('button', { name: 'Cancel' }, T)).toBeTruthy()
  })

  it('the way out without the hub is offered only from the offline banner', async () => {
    const view = hubPage('offline', '/w/DEMO')
    const banner = await screen.findByTestId('hub-offline-banner', {}, T)
    expect(within(banner).getByText('Hub offline, working locally')).toBeTruthy()
    await view.user.click(within(banner).getByTestId('hub-leave-down'))
    expect(await screen.findByText('Leave without the hub', {}, T)).toBeTruthy()
    cleanup()
    hubPage('selective', '/w/DEMO')
    await screen.findByTestId('hub-chip', {}, T)
    expect(screen.queryByTestId('hub-offline-banner')).toBeNull()
  })
})

describe('the hub admin asks for an update (HUB-WS-04)', () => {
  it('shows a plain notice with the version and the human-only way, and none without a request', async () => {
    hubPage('update', '/w/DEMO')
    const banner = await screen.findByTestId('hub-update-banner', {}, T)
    expect(within(banner).getByText('Your hub admin asks you to update orch to 0.2.0')).toBeTruthy()
    expect(within(banner).getByText('orch update')).toBeTruthy()
    cleanup()
    hubPage('full', '/w/DEMO')
    await screen.findByTestId('hub-chip', {}, T)
    expect(screen.queryByTestId('hub-update-banner')).toBeNull()
  })
})

describe('the hub is being removed (#1463)', () => {
  it('shows only our own words and Leave opens the normal leave review', async () => {
    const view = hubPage('closing', '/w/DEMO')
    const banner = await screen.findByTestId('hub-closing-banner', {}, T)
    expect(within(banner).getByText('Your company hub is being removed.')).toBeTruthy()
    await view.user.click(within(banner).getByTestId('hub-leave-closing'))
    expect(await screen.findByText('Leave the hub', {}, T)).toBeTruthy()
    expect(screen.queryByText('Leave without the hub')).toBeNull()
    cleanup()
    hubPage('full', '/w/DEMO')
    await screen.findByTestId('hub-chip', {}, T)
    expect(screen.queryByTestId('hub-closing-banner')).toBeNull()
  })
})

describe('stopping sharing one ticket', () => {
  it('reviews what stops and offers one Stop sharing button: no terminal command, no typed word, no backup key', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    await view.user.click(within(await screen.findByTestId('hub-ticket-list', {}, T)).getByTestId('hub-ticket-revoke-0'))
    const dialog = await screen.findByRole('dialog', { name: 'Stop sharing this ticket' }, T)
    expect(within(dialog).getByText(/The hub stops showing the ticket/)).toBeTruthy()
    expect(within(dialog).getByText(/cannot be taken back/)).toBeTruthy()
    expect(within(dialog).getByTestId('hub-revoke-ticket')).toHaveTextContent('“Export report as PDF”')
    await view.user.click(within(dialog).getByTestId('hub-revoke-details'))
    expect(within(dialog).getByTestId('hub-review-workspace')).toHaveTextContent(/^Workspace DEMO · [0-9a-z]{1,8}/)
    expect(await within(dialog).findByTestId('hub-revoke-stop', {}, T)).toHaveTextContent('Stop sharing')
    expect(within(dialog).queryByTestId('hub-revoke-unavailable')).toBeNull()
    expect(dialog).not.toHaveTextContent('One click is not ready')
    expect(within(dialog).queryByTestId('hub-command')).toBeNull()
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    expect(within(dialog).queryByTestId('hub-backup-input')).toBeNull()
    for (const code of document.querySelectorAll('code')) expect(code.textContent).not.toContain('orch hub')
    await view.user.click(within(dialog).getByRole('button', { name: 'Keep sharing' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Stop sharing this ticket' })).toBeNull(), T)
  })

  it('one press stops sharing, the dialog closes and the ticket leaves the list', async () => {
    vi.spyOn(gesture, 'trustedPress').mockReturnValue(true)
    const revoke = vi.spyOn(api, 'revokeHubShare')
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    await view.user.click(within(await screen.findByTestId('hub-ticket-list', {}, T)).getByTestId('hub-ticket-revoke-0'))
    const dialog = await screen.findByRole('dialog', { name: 'Stop sharing this ticket' }, T)
    const stop = await within(dialog).findByTestId('hub-revoke-stop', {}, T)
    await waitFor(() => expect(stop).toBeEnabled(), T)
    fireEvent.click(stop)
    fireEvent.click(stop)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Stop sharing this ticket' })).toBeNull(), T)
    expect(revoke).toHaveBeenCalledTimes(1)
    expect(revoke.mock.calls[0][1]).toBe('DEMO-0043')
    // the page behind the open dialog is aria-hidden: count the rows directly
    await waitFor(() => expect(screen.getByTestId('hub-ticket-list').querySelectorAll('li')).toHaveLength(1), T)
    vi.restoreAllMocks()
  })
})

describe('share rules (A3.3)', () => {
  it('previews a rule live, then reviews it with the honest caveat and no terminal command', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    expect(await screen.findByText('Choose what to share', {}, T)).toBeTruthy()
    expect(await screen.findAllByText('Always shared', {}, T)).toHaveLength(2)
    await view.user.click(screen.getByTestId('hub-rule-add'))
    await view.user.click((await screen.findByTestId('hub-rule-chips', {}, T)).querySelector('button')!)
    await waitFor(() => expect(screen.getByText(/1 now visible/)).toBeTruthy(), T)
    await view.user.click(screen.getByTestId('hub-share-review'))
    expect(await screen.findByTestId('hub-add-review', {}, T)).toBeTruthy()
    expect(screen.getByTestId('hub-add-later')).toHaveTextContent('Later edits')
    expect(screen.getByTestId('hub-add-sentence')).toHaveTextContent(/^Share \d+ \S+ tickets? from .+ with the hub/)
    expect(screen.getByTestId('hub-add-review').textContent).not.toMatch(/[0-9a-f]{32}/) // ids and the plan are behind Details
    expect(screen.queryByTestId('hub-add-hub')).toBeNull()
    expect(screen.getByTestId('hub-add-go')).toBeTruthy()
    expect(screen.queryByTestId('hub-command')).toBeNull()
    expect(screen.getAllByText(/cannot be taken back/).length).toBeGreaterThan(0)
  })

  it('the form asks who should see it (hub people and teams, nobody ticked by default) and ticks the description', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    await view.user.click(await screen.findByTestId('hub-rule-add', {}, T))
    expect(await screen.findByText('Who should see it?', {}, T)).toBeTruthy()
    expect(await screen.findByText('Ben Test', {}, T)).toBeTruthy()
    expect(screen.getByText(/Team:/)).toBeTruthy()
    expect(screen.getByTestId('hub-rule-only-me')).toHaveTextContent('Nobody else sees it until you pick someone.')
    expect(screen.getByTestId('hub-rule-description')).toBeChecked()
    const preview = vi.spyOn(api, 'getHubPreview')
    const picked = (await screen.findByTestId('hub-rule-chips', {}, T)).querySelector('button')!
    const value = picked.getAttribute('data-testid')!.replace('hub-rule-chip-', '')
    expect(value).not.toBe('')
    await view.user.click(picked)
    await view.user.click(screen.getByTestId('hub-rule-description')) // untick: the text stays home
    await view.user.click(await screen.findByTestId('hub-rule-team-finance', {}, T))
    await waitFor(() => expect(preview).toHaveBeenLastCalledWith(expect.anything(), { add: { kind: 'type', value, audience: { users: [], groups: ['finance'] }, description: false } }), T)
    expect(screen.getByTestId('hub-rule-only-me')).toHaveTextContent('not signed')
    vi.restoreAllMocks()
  })

  it('a value with shell characters is never put into a command', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    await view.user.click(await screen.findByTestId('hub-rule-add', {}, T))
    await view.user.click((await screen.findByTestId('hub-rule-chips', {}, T)).querySelector('button')!)
    await view.user.click(await screen.findByTestId('hub-share-review', {}, T).then((b) => { return b }))
    await waitFor(() => expect((screen.getByTestId('hub-share-review') as HTMLButtonElement).disabled).toBe(false), T)
    await view.user.click(screen.getByTestId('hub-share-review'))
    expect(await screen.findByTestId('hub-add-review', {}, T)).toBeTruthy()
    expect(screen.queryByTestId('hub-command')).toBeNull()
    for (const code of document.querySelectorAll('code')) expect(code.textContent).not.toContain('orch hub')
  })

  it('the tickets to stop sharing are listed inline with their buttons; only the keys are behind the i', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-choose-share', {}, T))
    const list = await screen.findByTestId('hub-ticket-list', {}, T)
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(within(list).getByTestId('hub-ticket-revoke-1')).toBeTruthy()
    expect(within(list).queryByText('DEMO-0045')).toBeNull()
    await view.user.click(within(list).getByTestId('hub-ticket-details-1'))
    expect(within(await screen.findByTestId('info-drawer-content', {}, T)).getByText('DEMO-0045')).toBeTruthy()
    await view.user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('info-drawer-content')).toBeNull(), T)
    await view.user.click(within(list).getByTestId('hub-ticket-revoke-1'))
    expect(await screen.findByRole('dialog', { name: 'Stop sharing this ticket' }, T)).toBeTruthy()
  })
})

describe('the Tickets Hub column (A5.6)', () => {
  it('marks Shared or Not shared in selective mode', async () => {
    hubPage('selective', '/w/DEMO/tickets')
    expect((await screen.findByTestId('ticket-hub-DEMO-0043', {}, T)).textContent).toBe('Shared')
    expect(screen.getByTestId('ticket-hub-DEMO-0042').textContent).toBe('Only here')
  })
  it('full mode: no banner (run 12a owner: the person chose Everything) and no marks', async () => {
    hubPage('full', '/w/DEMO/tickets')
    await screen.findByRole('table', { name: 'Tickets' }, T)
    expect(screen.queryByTestId('hub-all-tickets')).toBeNull()
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
    trustedPresses() // the armed Sign signs only on a trusted press (fireEvent is untrusted)
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
    expect(screen.getByTestId('hub-link-hub').textContent).not.toContain('9f3c')
    // The hub id is behind Details: hidden by default, there on press.
    expect(screen.getByTestId('hub-link-details').textContent).not.toContain('9f3c1ab277de40c8aa11bb22cc33dd44')
    await view.user.click(within(screen.getByTestId('hub-link-details')).getByText('Details'))
    expect(screen.getByTestId('hub-link-details').textContent).toContain('9f3c1ab277de40c8aa11bb22cc33dd44')
    await view.user.click(within(screen.getByTestId('hub-link-details')).getByText('Details'))
    await waitFor(() => expect(screen.getByTestId('hub-link-details').textContent).not.toContain('9f3c1ab277de40c8aa11bb22cc33dd44'), T)
    expect(screen.getByTestId('hub-link-hub').textContent).toContain('Only what I choose') // both choices are visible at once
    expect(screen.queryByText(/Notes that need your attention and full questions are always shared/)).toBeNull() // the long mode text is behind the i of the mode choice
    expect(screen.queryByText(/Review it here/)).toBeNull()
    expect(screen.queryByTestId('hub-card-text')).toBeNull() // no card file, no separate invite, no mode step
    await sign('Connect')
    expect(await screen.findByText('Connected · Hub sees only what you share', {}, T)).toBeTruthy() // the page behind says it
    expect(screen.queryByRole('dialog')).toBeNull() // and the dialog closed by itself: nothing left to press
    expect(seal).toHaveBeenCalledTimes(1)
    expect(seal.mock.calls[0][1]).toBe(WORDS)
    expect(connectHub).toHaveBeenCalledTimes(1) // the host tells the hub right after the signed join, with the link's code
    expect(connectHub.mock.calls[0][1]).toMatchObject({ invite: '7QK2M9XD', addr: '10.0.0.5:8105', idem: expect.stringMatching(/^a_[0-7][0-9A-HJKMNP-TV-Z]{25}$/) })
    expect(screen.queryByTestId('hub-command')).toBeNull() // no terminal command anywhere in this path
  })

  it('with the person key kept the join is one click and asks for no backup key, also in full mode', async () => {
    setMockVault(true)
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await pasteLink()
    await view.user.click(await screen.findByTestId('hub-mode-full', {}, T))
    const btn = await screen.findByRole('button', { name: 'Connect' }, T)
    expect(screen.queryByTestId('hub-backup-input')).toBeNull()
    await waitFor(() => expect(btn).toBeEnabled(), { timeout: 5000 })
    trustedPresses() // the armed Sign signs only on a trusted press (fireEvent is untrusted)
    fireEvent.pointerDown(btn)
    fireEvent.click(btn)
    expect(await screen.findByText('The hub can read everything in this workspace', {}, T)).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
    setMockVault(false)
  })

  it('with One click off, connecting says why and links to Settings', async () => {
    setMockOneClick(false)
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await pasteLink()
    expect(await screen.findByTestId('hub-no-one-click', {}, T)).toHaveTextContent('One click is not ready.')
    expect(screen.queryByTestId('hub-backup-input')).toBeNull()
    expect(screen.getByTestId('hub-settings-link')).toHaveTextContent('Turn on One click')
    expect(screen.queryByTestId('hub-join-terminal-instead')).toBeNull()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
  })

  it('a join the hub refuses is not "Connected": it says so, keeps the way out and can be tried again', async () => {
    setMockConnectError('The invite was not accepted.')
    const view = hubPage('standalone')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    await pasteLink()
    await sign('Connect')
    const failed = await screen.findByTestId('hub-connect-failed', {}, T)
    expect(failed.textContent).toContain("Couldn't connect. Try again.")
    // The reason is behind the i of the failure: hidden by default, there on press.
    expect(screen.queryByText(/The invite was not accepted\./)).toBeNull()
    expect(screen.getByTestId('hub-connect-stage').parentElement?.firstElementChild).toBe(screen.getByTestId('hub-connect-stage'))
    await view.user.click(within(failed).getByRole('button', { name: 'More information' }))
    expect((await screen.findByTestId('info-drawer-content', {}, T)).textContent).toMatch(/The invite was not accepted\./)
    await view.user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('info-drawer-content')).toBeNull(), T)
    expect(within(screen.getByRole('dialog')).queryByText(/^Connected\./)).toBeNull()
    setMockConnectError(null)
    await view.user.click(screen.getByTestId('hub-connect-retry'))
    expect(await screen.findByText('Connected · Hub sees only what you share', {}, T)).toBeTruthy()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('leaves: the consequences, then one click with the backup key', async () => {
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-leave', {}, T))
    expect(screen.getByText(/cannot be taken back/)).toBeTruthy()
    expect(screen.queryByTestId('hub-command')).toBeNull()
    await sign('Leave hub')
    expect(await screen.findByTestId('hub-step-done', {}, T)).toHaveTextContent(/You left the hub/)
    await view.user.click(within(screen.getByTestId('hub-step')).getByRole('button', { name: 'Close' }))
    expect(await screen.findByText(/Not connected\./, {}, T)).toBeTruthy()
  })

  it('leaves without the hub (offline banner): the same one click with the backup key, no terminal step', async () => {
    const prepare = vi.spyOn(api, 'prepareClick')
    const view = hubPage('offline', '/w/DEMO')
    await view.user.click(within(await screen.findByTestId('hub-offline-banner', {}, T)).getByTestId('hub-leave-down'))
    expect(await screen.findByText('Leave without the hub', {}, T)).toBeTruthy()
    expect(screen.queryByText(/One click is not ready/)).toBeNull()
    expect(screen.queryByTestId('hub-command')).toBeNull()
    await sign('Leave hub')
    expect(await screen.findByTestId('hub-step-done', {}, T)).toHaveTextContent(/You left the hub/)
    expect(prepare.mock.calls[0][1]).toEqual({ action: 'hub_leave' })
    expect(seal).toHaveBeenCalledTimes(1)
    expect(seal.mock.calls[0][1]).toBe(WORDS)
    expect(seal.mock.calls[0][2]).toMatchObject({ purpose: 'hub.backup_key' })
  })

  it('switches to full with the backup key when the person key is not kept: one click, sealed, then the hub is told', async () => {
    const connectHub = vi.spyOn(api, 'connectHub')
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-full-switch', {}, T))
    expect(screen.queryByTestId('hub-command')).toBeNull()
    await sign('Sign and let the hub read everything')
    await waitFor(() => expect(connectHub).toHaveBeenCalledTimes(1), T)
    expect(seal).toHaveBeenCalledTimes(1)
    expect(seal.mock.calls[0][1]).toBe(WORDS)
    expect(seal.mock.calls[0][2]).toMatchObject({ purpose: 'hub.backup_key' })
    expect(connectHub.mock.calls[0][1].invite).toBeUndefined()
  })

  it('with One click off, leaving links to Settings', async () => {
    setMockOneClick(false)
    const view = hubPage('selective')
    await view.user.click(await screen.findByTestId('hub-leave', {}, T))
    expect(await screen.findByTestId('hub-no-one-click', {}, T)).toHaveTextContent('One click is not ready.')
    expect(screen.getByTestId('hub-settings-link')).toHaveTextContent('Turn on One click')
    expect(screen.queryByTestId('hub-leave-terminal')).toBeNull()
  })
})
