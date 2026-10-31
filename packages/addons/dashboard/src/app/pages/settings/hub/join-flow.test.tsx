// P06: the join dialog says "Connected" and closes only after the hub has confirmed its first settings.
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import { toast } from 'sonner'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import * as sealed from '@/api/sealed'
import * as gesture from '@/components/sign/trustedPress'
import { setHubScenario, setMockConfirmAfter, setMockConnectError, setMockOneClick, setMockVault } from '@/mocks/hub'
import { trustedPresses } from '@/test/hostHarness'
import { renderApp } from '@/test/renderApp'

void gesture
const T = { timeout: 4000 }
const HUB_NAME = 'Acme Hub'
const LINK = 'http://10.0.0.5:8106/c#h1.9f3c1ab277de40c8aa11bb22cc33dd44.7QK2M9XD.10.0.0.5:8105'
beforeAll(async () => { await Promise.all([import('@/app/pages/settings'), import('@/app/pages/tickets')]) }, 60000)
beforeEach(() => {
    setHubScenario('standalone'); setMockOneClick(true); setMockVault(true); setMockConnectError(null); setMockConfirmAfter(0)
  vi.spyOn(sealed, 'seal').mockResolvedValue({ v: 1, alg: 'P256-HKDF-SHA256-A256GCM', epk: 'e', ct: 'c' } as sealed.Envelope)
})
afterEach(() => { cleanup(); setHubScenario(null); setMockConfirmAfter(0); vi.restoreAllMocks() })

async function joinUntilSigned(beforeSign?: () => void) {
  const view = renderApp('/w/DEMO/settings/hub')
  await view.user.click(await screen.findByTestId('hub-connect', {}, T))
  fireEvent.change(await screen.findByTestId('hub-link-text', {}, T), { target: { value: LINK } })
  const btn = await screen.findByRole('button', { name: 'Connect' }, T)
  await waitFor(() => expect(btn).toBeEnabled(), { timeout: 5000 })
  beforeSign?.()
  trustedPresses()
  fireEvent.pointerDown(btn)
  fireEvent.click(btn)
  return view
}

describe('the join waits for the hub', () => {
  it('stays open while the hub has not confirmed, then Check again confirms, toasts and closes', async () => {
    setMockConfirmAfter(1)
    const success = vi.spyOn(toast, 'success')
    const refresh = vi.spyOn(api, 'refreshHub')
    const hubStatus = vi.spyOn(api, 'getHubStatus')
    await joinUntilSigned()
    expect((await screen.findByTestId('hub-connect-waiting', {}, T)).textContent).toContain('Connected, but the hub has not confirmed yet.')
    const reads = hubStatus.mock.calls.length
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.queryByTestId('hub-mode-selective')).toBeNull() // once signed the mode can no longer be changed
    expect(screen.queryByTestId('hub-mode-full')).toBeNull()
    expect(success).not.toHaveBeenCalled() // no "Connected" before the hub said so
    expect(refresh).not.toHaveBeenCalled()
    expect(hubStatus.mock.calls.length).toBe(reads) // and the page behind is not read while the add-on is paused
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(success).toHaveBeenCalledTimes(1)
  })

  it('a hub that is still silent keeps the dialog open with the same button', async () => {
    setMockConfirmAfter(99)
    const success = vi.spyOn(toast, 'success')
    await joinUntilSigned()
    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }, T))
    await waitFor(() => expect(screen.getByTestId('hub-connect-waiting')).toBeTruthy(), T)
    expect(screen.getByRole('button', { name: 'Check again' })).toBeTruthy()
    expect(success).not.toHaveBeenCalled()
  })

  it('a real error is said in plain words, without the code prefix', async () => {
    setMockConnectError('The hub is not reachable.')
    await joinUntilSigned()
    const failed = await screen.findByTestId('hub-connect-failed', {}, T)
    expect(failed.textContent).toContain("Couldn't connect. Try again.")
    expect(failed.textContent).not.toContain('hub.unreachable')
  })
})

describe('P18: nothing behind the dialog is read before the hub confirmed', () => {
  it('Today and Agent access are not read after the signature, only once after "Connected"', async () => {
    setMockConfirmAfter(1)
    const today = vi.spyOn(api, 'getToday')
    const grants = vi.spyOn(api, 'listHostGrants')
    let before = [0, 0]
    await joinUntilSigned(() => { before = [today.mock.calls.length, grants.mock.calls.length] })
    await screen.findByTestId('hub-connect-waiting', {}, T)
    expect([today.mock.calls.length, grants.mock.calls.length]).toEqual(before) // signed, hub silent: no read, so no 409
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
    await waitFor(() => expect(today.mock.calls.length).toBeGreaterThan(before[0]), T) // read once the hub confirmed
  })
})

describe('P18: closing while the hub is silent', () => {
  it('reads the hub status again when the person closes the waiting dialog', async () => {
    setMockConfirmAfter(99)
    const hubStatus = vi.spyOn(api, 'getHubStatus')
    await joinUntilSigned()
    await screen.findByTestId('hub-connect-waiting', {}, T)
    const reads = hubStatus.mock.calls.length
    fireEvent.click(screen.getByTestId('hub-connect-close'))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
    await waitFor(() => expect(hubStatus.mock.calls.length).toBeGreaterThan(reads), T)
  })
})

describe('P10: a short join dialog', () => {
  const open = async () => {
    const view = renderApp('/w/DEMO/settings/hub')
    await view.user.click(await screen.findByTestId('hub-connect', {}, T))
    return view
  }
  it('the Standalone page does not mention a backup key and the link hint is one sentence', async () => {
    const view = await open()
    expect(document.body.textContent).not.toMatch(/backup key/i)
    expect(document.body.textContent).toContain('Paste the connect link from your hub admin')
    // P08: nothing to unfold: the field is there at once with the clipboard button beside it
    expect(await screen.findByRole('button', { name: 'Paste from clipboard' }, T)).toBeTruthy()
    expect(screen.getByLabelText('Paste the connect link from your hub admin')).toBe(screen.getByTestId('hub-link-text'))
    expect(screen.getByTestId('hub-link-text')).toBeVisible()
    expect(screen.getByTestId('hub-link-paste').closest('details')).toBeNull()
    expect(screen.queryByText('Paste a different link')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Use copied link' })).toBeNull()
    view.unmount()
  })
  it('shows both choices at once with the first chosen, no Change link, and the caveat only for Everything', async () => {
    const view = await open()
    fireEvent.change(await screen.findByTestId('hub-link-text', {}, T), { target: { value: LINK } })
    const hub = await screen.findByTestId('hub-link-hub', {}, T)
    expect(screen.getByText('What can the hub see?')).toBeTruthy()
    expect(screen.getByTestId('hub-mode-selective').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('hub-mode-full').getAttribute('aria-pressed')).toBe('false')
    expect(screen.queryByTestId('hub-mode-other')).toBeNull()
    expect(screen.queryByText('Change')).toBeNull()
    expect(screen.getByTestId('hub-link-later').textContent).toBe('Leaving later does not take back what the hub already read.')
    // one question naming the hub by its address, one Details fold in the hub box; Connect stays the only way on
    expect(hub.textContent).toContain('Connect to 10.0.0.5:8106?')
    expect(hub.querySelectorAll('details')).toHaveLength(1)
    expect(screen.getByTestId('hub-link-found').textContent).toBe('Link found: 10.0.0.5:8106')
    expect(screen.queryByText('What the hub has already read cannot be taken back.')).toBeNull()
    await view.user.click(screen.getByTestId('hub-mode-full'))
    expect(screen.getByText('What the hub has already read cannot be taken back.')).toBeTruthy()
    expect(screen.queryByTestId('hub-link-later')).toBeNull()
    await view.user.click(screen.getByTestId('hub-mode-selective'))
    expect(screen.queryByText('What the hub has already read cannot be taken back.')).toBeNull()
  })
})

// r9 P13: the hub's /c page opens the workspace with the link in the address fragment; the join dialog is prefilled.
describe('a connect link in the address fragment', () => {
  it('opens the join dialog with the hub looked up, without typing, and removes the link from the address', async () => {
    const view = renderApp('/w/DEMO/settings/hub#connect=' + encodeURIComponent(LINK))
    expect((screen.queryByTestId('hub-link-hub'))).toBeNull()
    expect((await screen.findByTestId('hub-link-text', {}, T) as HTMLInputElement).value).toBe(LINK)
    expect(screen.getByTestId('hub-link-from').textContent).toContain('http://10.0.0.5:8106') // no lookup yet: the target is named
    await view.user.click(await screen.findByRole('button', { name: 'Look up this hub' }, T))
    expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('Connect to 10.0.0.5:8106?')
    await waitFor(() => expect(view.address()).not.toContain('connect'), T)
    view.unmount()
  })
  it('ignores a fragment that is not a connect link: no dialog, nothing prefilled', async () => {
    const view = renderApp('/w/DEMO/settings/hub#connect=' + encodeURIComponent('javascript:alert(1)'))
    await screen.findByTestId('hub-connect', {}, T)
    expect(screen.queryByTestId('hub-link-text')).toBeNull()
    expect(view.address()).not.toContain('connect')
    view.unmount()
  })
})

// P12: "Use copied link" reads the clipboard only on a click, validates it with LINK and never signs by itself.
describe('P12 / P08: Paste from clipboard', () => {
  const clip = (read: () => Promise<string>) => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { readText: vi.fn(read) } })
  const open = async () => { const view = renderApp('/w/DEMO/settings/hub'); await view.user.click(await screen.findByTestId('hub-connect', {}, T)); return view }
  afterEach(() => { Reflect.deleteProperty(navigator, 'clipboard') })
  it('does not read the clipboard until the button is pressed, then fills the link and looks the hub up, without signing', async () => {
    const join = vi.spyOn(api, 'connectHub')
    const view = await open()
    clip(async () => LINK) // after render: user-event installs its own clipboard stub in renderApp
    expect(navigator.clipboard.readText).not.toHaveBeenCalled()
    expect(screen.getByTestId('hub-link-text').getAttribute('placeholder')).toBeNull()
    await view.user.click(await screen.findByRole('button', { name: 'Paste from clipboard' }, T))
    expect((screen.getByTestId('hub-link-text') as HTMLInputElement).value).toBe(LINK)
    expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('Connect to 10.0.0.5:8106?') // P08: the hub is named
    expect(navigator.clipboard.readText).toHaveBeenCalledTimes(1)
    expect(join).not.toHaveBeenCalled() // the person still presses Connect
    view.unmount()
  })
  it('junk on the clipboard fills nothing and says so in plain words', async () => {
    const view = await open()
    clip(async () => 'javascript:alert(1)')
    await view.user.click(await screen.findByRole('button', { name: 'Paste from clipboard' }, T))
    expect((await screen.findByTestId('hub-link-nocopied', {}, T)).textContent).toBe('Copy the link in the hub first.')
    expect((screen.getByTestId('hub-link-text') as HTMLInputElement).value).toBe('')
    view.unmount()
  })
  it('a refused clipboard read leaves the paste field as the way in', async () => {
    const view = await open()
    clip(async () => { throw new Error('denied') })
    await view.user.click(await screen.findByRole('button', { name: 'Paste from clipboard' }, T))
    expect(await screen.findByTestId('hub-link-nocopied', {}, T)).toBeTruthy()
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: LINK } })
    expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('Connect to 10.0.0.5:8106?') // P08: the hub is named
    view.unmount()
  })
  it('with clipboard permission already granted the copied link only fills the field: no lookup until the click', async () => {
    const lookup = vi.spyOn(api, 'getHubLink')
    const join = vi.spyOn(api, 'connectHub')
    Object.defineProperty(navigator, 'permissions', { configurable: true, value: { query: vi.fn(async () => ({ state: 'granted' })) } })
    try {
      const view = renderApp('/w/DEMO/settings/hub')
      clip(async () => LINK)
      await view.user.click(await screen.findByTestId('hub-connect', {}, T))
      await waitFor(() => expect((screen.getByTestId('hub-link-text') as HTMLInputElement).value).toBe(LINK), T)
      expect(screen.getByTestId('hub-link-from').textContent).toContain('http://10.0.0.5:8106') // the host is shown plainly
      expect(lookup).not.toHaveBeenCalled()
      expect(screen.queryByTestId('hub-link-hub')).toBeNull()
      await view.user.click(await screen.findByRole('button', { name: 'Look up this hub' }, T))
      expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('Connect to 10.0.0.5:8106?')
      expect(lookup).toHaveBeenCalledTimes(1)
      expect(lookup.mock.calls[0][1]).toBe(LINK)
      expect(join).not.toHaveBeenCalled()
      view.unmount()
    } finally { Reflect.deleteProperty(navigator, 'permissions') }
  })
  it('P08: the clipboard button reads only on its click; the address that was looked up sits under Details', async () => {
    const view = await open()
    clip(async () => LINK)
    expect(navigator.clipboard.readText).not.toHaveBeenCalled()
    await view.user.click(await screen.findByRole('button', { name: 'Paste from clipboard' }, T))
    const hub = await screen.findByTestId('hub-link-hub', {}, T)
    expect(hub.textContent).toContain('Connect to 10.0.0.5:8106?')
    await view.user.click(hub.querySelector('summary') as HTMLElement)
    expect(screen.getByTestId('hub-link-details').textContent).toContain('Address: http://10.0.0.5:8106')
    view.unmount()
  })
  it('P08: the consent line carries the verified host even when the hub names itself like another host', async () => {
    vi.spyOn(api, 'getHubLink').mockImplementation(async (_ws, link) => ({ hub_id: 'a'.repeat(32), name: 'hub.acme.com', code: 'c', endpoint: null, dev: true, hub_card: '{}', owner_user: null, link } as never))
    const view = await open()
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: LINK } })
    expect((await screen.findByTestId('hub-link-question', {}, T)).textContent).toBe('Connect to hub.acme.com at 10.0.0.5:8106?')
    view.unmount()
  })
  it('P08: a real distinct name reads "<name> at <host>"; a name that is only the address (the real shape) reads "<host>" once', async () => {
    const real = api.getHubLink.bind(api)
    vi.spyOn(api, 'getHubLink').mockImplementation(async (ws, link) => ({ ...(await real(ws, link)), name: HUB_NAME }))
    const view = await open()
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: LINK } })
    expect((await screen.findByTestId('hub-link-question', {}, T)).textContent).toBe('Connect to Acme Hub at 10.0.0.5:8106?')
    expect(screen.getByTestId('hub-link-found').textContent).toBe('Link found: Acme Hub')
    view.unmount()
  })
  it('P08: the name is the address in other spellings (case, :443): single-host wording', async () => {
    vi.spyOn(api, 'getHubLink').mockImplementation(async () => ({ hub_id: 'a'.repeat(32), name: 'Hub.Example.org:443', code: 'c', endpoint: null, dev: true, hub_card: '{}', owner_user: null } as never))
    const view = await open()
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: 'https://hub.example.org/c#h1.9f3c1ab277de40c8aa11bb22cc33dd44.7QK2M9XD' } })
    expect((await screen.findByTestId('hub-link-question', {}, T)).textContent).toBe('Connect to hub.example.org?')
    view.unmount()
  })
  it('P08: no name: the question is the host; the port 443 is not shown', async () => {
    vi.spyOn(api, 'getHubLink').mockImplementation(async () => ({ hub_id: 'a'.repeat(32), name: ' ', code: 'c', endpoint: null, dev: true, hub_card: '{}', owner_user: null } as never))
    const view = await open()
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: 'https://hub.example.org:443/c#h1.9f3c1ab277de40c8aa11bb22cc33dd44.7QK2M9XD' } })
    expect((await screen.findByTestId('hub-link-question', {}, T)).textContent).toBe('Connect to hub.example.org?')
    view.unmount()
  })
  it('P08: while the hub is looked up it says so; editing the link drops the shown hub until the new link is looked up', async () => {
    vi.spyOn(api, 'getHubLink')
    const view = await open()
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: LINK } })
    await screen.findByTestId('hub-link-hub', {}, T)
    const other = 'http://10.0.0.9:9/c#h1.00000000000000000000000000000000.AAAAAAAA'
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: other } })
    expect(screen.queryByTestId('hub-link-hub')).toBeNull() // the old hub's name never stands next to another link
    expect(screen.queryByTestId('hub-link-found')).toBeNull()
    await waitFor(() => expect(api.getHubLink).toHaveBeenLastCalledWith(expect.anything(), other), T)
    view.unmount()
  })
  it('the automatic clipboard read never replaces a link the person already typed', async () => {
    let grant: (v: { state: string }) => void = () => {}
    Object.defineProperty(navigator, 'permissions', { configurable: true, value: { query: vi.fn(() => new Promise(r => { grant = r })) } })
    try {
      const view = renderApp('/w/DEMO/settings/hub')
      clip(async () => LINK)
      await view.user.click(await screen.findByTestId('hub-connect', {}, T))
      const mine = 'http://10.0.0.9:9/c#h1.00000000000000000000000000000000.AAAAAAAA'
      fireEvent.change(await screen.findByTestId('hub-link-text', {}, T), { target: { value: mine } })
      grant({ state: 'granted' })
      await new Promise(r => setTimeout(r, 50))
      expect((screen.getByTestId('hub-link-text') as HTMLInputElement).value).toBe(mine)
      view.unmount()
    } finally { Reflect.deleteProperty(navigator, 'permissions') }
  })
  it('without permission the field and the button are both there at once, nothing folded, nothing read', async () => {
    Object.defineProperty(navigator, 'permissions', { configurable: true, value: { query: vi.fn(async () => ({ state: 'prompt' })) } })
    try {
      const view = await open()
      clip(async () => LINK)
      expect(await screen.findByRole('button', { name: 'Paste from clipboard' }, T)).toBeTruthy()
      expect(screen.getByTestId('hub-link-text')).toBeVisible()
      expect(screen.getByTestId('hub-link-paste').closest('details')).toBeNull()
      expect(navigator.clipboard.readText).not.toHaveBeenCalled()
      view.unmount()
    } finally { Reflect.deleteProperty(navigator, 'permissions') }
  })
})
