import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Workspace } from '@/api/types'
import { HostDevices } from './Devices'

const hub = vi.hoisted(() => ({ data: undefined as unknown, isPending: false, fetchStatus: 'idle' }))
vi.mock('../hub/shared', () => ({ useHub: () => hub }))
vi.mock('./Readers', () => ({ ReaderLevels: () => null }))
vi.mock('./ReplaceKey', () => ({ ReplaceKey: () => null }))
const hubUrl = vi.hoisted(() => vi.fn())
vi.mock('@/api/switch', () => ({ hubUrl }))
vi.mock('@/app/toast', () => ({ toastApiError: vi.fn() }))

const ws = { id: 'w1', prefix: 'T', name: 'Test' } as unknown as Workspace
const assign = vi.fn()

beforeEach(() => {
  hubUrl.mockReset().mockResolvedValue('https://hub.example/#t=abc')
  assign.mockReset()
  Object.defineProperty(window, 'location', { value: { assign }, writable: true, configurable: true })
})
afterEach(() => cleanup())

describe('Settings > Devices: phones connect through the hub', () => {
  it('joined to a hub: one button opens the hub Phones page through the hub switch', async () => {
    hub.data = { joined: true, mode: 'selective' }
    render(<HostDevices workspace={ws} />)
    fireEvent.click(screen.getByTestId('add-phone'))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('https://hub.example/#t=abc'))
    expect(hubUrl).toHaveBeenCalledWith('w1', 'open', '/phones')
    expect(screen.queryByTestId('devices-needs-hub')).toBeNull()
  })

  it('no hub: one quiet line with an i, no button, no pairing promise', () => {
    hub.data = { joined: false, mode: 'standalone' }
    render(<HostDevices workspace={ws} />)
    expect(screen.getByTestId('devices-needs-hub')).toHaveTextContent('Phones connect through your company hub')
    expect(screen.queryByTestId('add-phone')).toBeNull()
    expect(screen.queryByRole('button', { name: /pair/i })).toBeNull()
    expect(document.body.textContent).not.toMatch(/terminal|comes later|relay|QR/i)
  })

  it('the hub could not be reached: the button comes back, nothing navigates', async () => {
    hub.data = { joined: true, mode: 'full' }
    hubUrl.mockRejectedValue(new Error('x'))
    render(<HostDevices workspace={ws} />)
    fireEvent.click(screen.getByTestId('add-phone'))
    await waitFor(() => expect(screen.getByTestId('add-phone')).toBeEnabled())
    expect(assign).not.toHaveBeenCalled()
  })
})
