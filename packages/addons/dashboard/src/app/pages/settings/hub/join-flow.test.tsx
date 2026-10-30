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
    expect(screen.queryByTestId('hub-mode-other')).toBeNull() // once signed the mode can no longer be changed
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
    expect(document.body.textContent).toContain('Paste the connect link from your hub admin.')
    expect(document.activeElement).toBe(await screen.findByTestId('hub-link-text', {}, T))
    view.unmount()
  })
  it('shows one mode box, the choice only after "Change", and the caveat only for Everything', async () => {
    const view = await open()
    fireEvent.change(await screen.findByTestId('hub-link-text', {}, T), { target: { value: LINK } })
    const box = await screen.findByTestId('hub-mode-box', {}, T)
    expect(box.textContent).toContain("The hub sees the tickets you pick, notes that need you, and this workspace's name.")
    // one question naming the hub by its address, one Details fold in the hub box; Connect stays the only way on
    expect(screen.getByTestId('hub-link-hub').textContent).toContain('Connect to the hub at')
    expect(screen.getByTestId('hub-link-hub').querySelectorAll('details')).toHaveLength(1)
    expect(screen.queryByTestId('hub-link-hub')!.contains(box)).toBe(true)
    expect(screen.queryByTestId('hub-mode-full')).toBeNull()
    expect(screen.queryByText('What the hub has already read cannot be taken back.')).toBeNull()
    expect(screen.queryByText('You choose what to share.')).toBeNull()
    await view.user.click(screen.getByTestId('hub-mode-other'))
    expect(screen.getByText('What the hub can see')).toBeTruthy()
    expect(screen.queryByTestId('hub-mode-box')).toBeNull()
    await view.user.click(screen.getByTestId('hub-mode-full'))
    expect(screen.getByText('What the hub has already read cannot be taken back.')).toBeTruthy()
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
    expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('Connect to the hub at')
    expect(screen.getByTestId('hub-link-hub').textContent).toContain('Connect to the hub at 10.0.0.5:8106?')
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
describe('P12: Use copied link', () => {
  const clip = (read: () => Promise<string>) => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { readText: vi.fn(read) } })
  const open = async () => { const view = renderApp('/w/DEMO/settings/hub'); await view.user.click(await screen.findByTestId('hub-connect', {}, T)); return view }
  afterEach(() => { Reflect.deleteProperty(navigator, 'clipboard') })
  it('does not read the clipboard until the button is pressed, then fills the link and looks the hub up, without signing', async () => {
    const join = vi.spyOn(api, 'connectHub')
    const view = await open()
    clip(async () => LINK) // after render: user-event installs its own clipboard stub in renderApp
    expect(navigator.clipboard.readText).not.toHaveBeenCalled()
    expect(screen.getByTestId('hub-link-text').getAttribute('placeholder')).toBeNull()
    await view.user.click(await screen.findByRole('button', { name: 'Use copied link' }, T))
    expect((screen.getByTestId('hub-link-text') as HTMLInputElement).value).toBe(LINK)
    expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('Connect to the hub at') // r14 P07: the address is the question; the hub name sits under Details
    expect(navigator.clipboard.readText).toHaveBeenCalledTimes(1)
    expect(join).not.toHaveBeenCalled() // the person still presses Connect
    view.unmount()
  })
  it('junk on the clipboard fills nothing and says so in plain words', async () => {
    const view = await open()
    clip(async () => 'javascript:alert(1)')
    await view.user.click(await screen.findByRole('button', { name: 'Use copied link' }, T))
    expect((await screen.findByTestId('hub-link-nocopied', {}, T)).textContent).toBe('There is no connect link copied yet.')
    expect((screen.getByTestId('hub-link-text') as HTMLInputElement).value).toBe('')
    view.unmount()
  })
  it('a refused clipboard read leaves the paste field as the way in', async () => {
    const view = await open()
    clip(async () => { throw new Error('denied') })
    await view.user.click(await screen.findByRole('button', { name: 'Use copied link' }, T))
    expect(await screen.findByTestId('hub-link-nocopied', {}, T)).toBeTruthy()
    fireEvent.change(screen.getByTestId('hub-link-text'), { target: { value: LINK } })
    expect((await screen.findByTestId('hub-link-hub', {}, T)).textContent).toContain('Connect to the hub at') // r14 P07: the address is the question; the hub name sits under Details
    view.unmount()
  })
})
