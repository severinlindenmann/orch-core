// R2 T4 (#1590): Settings > Devices > Remote access. The api is faked at its boundary.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { RemoteView } from '@/api/remote'
import { TooltipProvider } from '@/components/ui/tooltip'
import { RemoteAccess } from './RemoteAccess'

const WS = '705d40abbb8c1c90354a1acaa94c935c'
const DEV = 'rd_' + 'a'.repeat(32)
const PENDING = 'rp_' + 'b'.repeat(32)
const REQ = 'hr_01M5PVRB46WS3X8H5X2RQK04S9'
const OFF: RemoteView = { on: false, level: null, until_ms: null, premises: null, off_reason: null, isolated: true, hub_mode: 'none', devices: [], pending: [], sessions: [] }
const ON: RemoteView = {
  ...OFF, on: true, level: 'work', until_ms: Date.now() + 86_400_000, premises: 'no',
  devices: [{ device_id: DEV, name: "Anna's laptop", level_cap: 'view', added_ms: 1, expires_ms: Date.now() + 86_400_000, words: 'one two three four five six' }],
}

let view: RemoteView
let spies: Record<string, ReturnType<typeof vi.spyOn>>
beforeEach(() => {
  view = OFF
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  spies = {
    get: vi.spyOn(api, 'getRemote').mockImplementation(async () => view),
    off: vi.spyOn(api, 'remoteOff').mockResolvedValue({ changed: true }),
    code: vi.spyOn(api, 'remoteCode').mockResolvedValue({ code: 'orch-rc1.w.AAAA.BBBB', expires_ms: 1 }),
    remove: vi.spyOn(api, 'removeRemoteDevice').mockResolvedValue({ removed: true }),
    click: vi.spyOn(api, 'clickStatus').mockResolvedValue({ custody: 'convenience', enabled: true, available: true, switchable: true } as never),
    prepare: vi.spyOn(api, 'prepareClick').mockRejectedValue(new Error('stop here')),
    workspaces: vi.spyOn(api, 'getWorkspaces').mockResolvedValue([] as never),
  }
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><TooltipProvider><RemoteAccess ws={WS} /></TooltipProvider></QueryClientProvider>)
}
const T = { timeout: 5000 }

describe('Remote access, off', () => {
  it('says Off with one Turn on button; nothing else is offered', async () => {
    mount()
    expect(await screen.findByTestId('remote-state', {}, T)).toHaveTextContent('Remote access: Off')
    expect(screen.getByTestId('remote-turn-on-btn')).toBeInTheDocument()
    expect(screen.queryByTestId('remote-more')).toBeNull()
    expect(screen.queryByTestId('remote-add-device')).toBeNull()
    expect(document.body.textContent).not.toMatch(/netbird/i)
  })

  it('not isolated: Turn on sits behind More, with the agents line', async () => {
    view = { ...OFF, isolated: false }
    mount()
    await screen.findByTestId('remote-state', {}, T)
    expect(screen.queryByTestId('remote-turn-on-btn')).toBeNull()
    expect(screen.queryByText('Agents on this computer can act as you')).toBeNull()
    fireEvent.click(screen.getByTestId('remote-more'))
    expect(screen.getByText('Agents on this computer can act as you')).toBeInTheDocument()
    expect(screen.getByTestId('remote-turn-on-btn')).toBeInTheDocument()
  })

  it('asks one question, never preselects Terminals, and cannot continue before the answer', async () => {
    mount()
    fireEvent.click(await screen.findByTestId('remote-turn-on-btn', {}, T))
    expect(screen.getByText("Does this computer run at a customer's place?")).toBeInTheDocument()
    expect(screen.getByTestId('remote-level-terminals')).not.toBeChecked()
    expect(screen.getByTestId('remote-level-view')).toBeChecked()
    await waitFor(() => expect(spies.click).toHaveBeenCalled())
    expect(screen.getByTestId('remote-sign')).toBeDisabled()
    fireEvent.click(screen.getByTestId('remote-premises-no'))
    await waitFor(() => expect(screen.getByTestId('remote-sign')).toBeEnabled())
  })

  it('"Until I turn it off" only for No and not for Terminals', async () => {
    mount()
    fireEvent.click(await screen.findByTestId('remote-turn-on-btn', {}, T))
    expect(screen.queryByTestId('remote-days-forever')).toBeNull() // no answer yet
    fireEvent.click(screen.getByTestId('remote-premises-no'))
    expect(screen.getByTestId('remote-days-forever')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('remote-premises-yes'))
    expect(screen.queryByTestId('remote-days-forever')).toBeNull()
    fireEvent.click(screen.getByTestId('remote-premises-no'))
    fireEvent.click(screen.getByTestId('remote-days-forever'))
    fireEvent.click(screen.getByTestId('remote-level-terminals'))
    expect(screen.queryByTestId('remote-days-forever')).toBeNull()
    expect(screen.getByTestId('remote-days-3')).toBeChecked() // falls back to a number of days, never to no end
  })

  it('turning on needs the signing step: Continue asks the signer for a remote_set with exactly the choices', async () => {
    mount()
    fireEvent.click(await screen.findByTestId('remote-turn-on-btn', {}, T))
    fireEvent.click(screen.getByTestId('remote-premises-yes'))
    fireEvent.click(screen.getByTestId('remote-level-work'))
    fireEvent.click(screen.getByTestId('remote-days-7'))
    await waitFor(() => expect(screen.getByTestId('remote-sign')).toBeEnabled())
    fireEvent.click(screen.getByTestId('remote-sign'))
    await waitFor(() => expect(spies.prepare).toHaveBeenCalled())
    expect(spies.prepare.mock.calls[0]![1]).toEqual({ action: 'remote_set', level: 'work', premises: 'yes', days: 7 })
    expect(spies.off).not.toHaveBeenCalled() // nothing was changed without the signer
  })

  it('not isolated: the agents risk is part of what is signed', async () => {
    view = { ...OFF, isolated: false }
    mount()
    await screen.findByTestId('remote-state', {}, T)
    fireEvent.click(screen.getByTestId('remote-more'))
    fireEvent.click(screen.getByTestId('remote-turn-on-btn'))
    fireEvent.click(screen.getByTestId('remote-premises-no'))
    fireEvent.click(screen.getByTestId('remote-days-forever'))
    await waitFor(() => expect(screen.getByTestId('remote-sign')).toBeEnabled())
    fireEvent.click(screen.getByTestId('remote-sign'))
    await waitFor(() => expect(spies.prepare).toHaveBeenCalled())
    expect(spies.prepare.mock.calls[0]![1]).toEqual({ action: 'remote_set', level: 'view', premises: 'no', agents_risk: true })
  })

  it('the hub reads this workspace: a customer place cannot continue', async () => {
    view = { ...OFF, hub_mode: 'full' }
    mount()
    fireEvent.click(await screen.findByTestId('remote-turn-on-btn', {}, T))
    fireEvent.click(screen.getByTestId('remote-premises-yes'))
    expect(screen.getByTestId('remote-hub-full')).toBeInTheDocument()
    expect(screen.getByTestId('remote-sign')).toBeDisabled()
  })
})

describe('Remote access, on', () => {
  beforeEach(() => { view = ON })

  it('lists devices and sessions; ids are behind the small i', async () => {
    mount()
    expect(await screen.findByTestId('remote-state', {}, T)).toHaveTextContent('Remote access: On')
    expect(screen.getByTestId('remote-device')).toHaveTextContent("Anna's laptop")
    expect(screen.getByTestId('remote-no-sessions')).toHaveTextContent('No one is connected.')
    expect(document.body.textContent).not.toContain(DEV)
  })

  it('removing a device is one click', async () => {
    mount()
    fireEvent.click(await screen.findByTestId('remote-device-remove', {}, T))
    await waitFor(() => expect(spies.remove).toHaveBeenCalledWith(WS, DEV))
    expect(document.querySelector('input[type=text], input[type=password]')).toBeNull() // nothing to type
  })

  it('turning off is one click', async () => {
    mount()
    fireEvent.click(await screen.findByTestId('remote-turn-off', {}, T))
    await waitFor(() => expect(spies.off).toHaveBeenCalledWith(WS))
  })

  it('Add a device shows the code with Copy', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    mount()
    fireEvent.click(await screen.findByTestId('remote-add-device', {}, T))
    expect(await screen.findByTestId('remote-code', {}, T)).toHaveTextContent('orch-rc1.w.AAAA.BBBB')
    fireEvent.click(screen.getByTestId('remote-code-copy'))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('orch-rc1.w.AAAA.BBBB'))
  })

  it('a waiting device asks "Add device <name>, fingerprint <words>?" and is added through the signer', async () => {
    view = { ...ON, pending: [{ pending: PENDING, name: "Anna's phone", words: 'alpha bravo charlie delta echo foxtrot', expires_ms: 1 }] }
    mount()
    expect(await screen.findByTestId('remote-pending', {}, T)).toHaveTextContent("Add device Anna's phone, fingerprint alpha bravo charlie delta echo foxtrot?")
    expect(screen.queryByTestId('remote-pending-level-terminals')).toBeNull() // work policy: Terminals is not even offered
    fireEvent.click(screen.getByTestId('remote-pending-days-90'))
    fireEvent.click(screen.getByTestId('remote-pending-add'))
    await waitFor(() => expect(spies.prepare).toHaveBeenCalled())
    expect(spies.prepare.mock.calls[0]![1]).toEqual({ action: 'remote_device_confirm', pending: PENDING, level_cap: 'view', days: 90 })
  })
})
