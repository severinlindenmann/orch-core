import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as passkey from '@/api/passkey'
import { PasskeyError } from '@/api/passkey'
import { EnrollPage } from './EnrollPage'
import { LoginGate } from './LoginPage'

vi.mock('@/api/passkey', async (orig) => ({ ...(await orig<typeof import('@/api/passkey')>()), prepareLogin: vi.fn(), signIn: vi.fn(), enrollDevice: vi.fn(), passkeysSupported: vi.fn(() => true) }))
const m = <T extends keyof typeof passkey>(k: T) => passkey[k] as unknown as ReturnType<typeof vi.fn>
const LOCAL = <p>Run orch dashboard in your terminal</p>

beforeEach(() => { vi.clearAllMocks(); m('passkeysSupported').mockReturnValue(true); m('prepareLogin').mockResolvedValue(true) })
afterEach(() => vi.useRealTimers())

describe('login', () => {
  it('on this computer\'s own address shows the old words, not a passkey button', async () => {
    m('prepareLogin').mockResolvedValue(false)
    render(<LoginGate local={LOCAL} />)
    expect(await screen.findByText(/Run orch dashboard/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /passkey/ })).toBeNull()
  })
  it('shows one button, no field, and the way back in only behind folds', async () => {
    render(<LoginGate local={LOCAL} />)
    const button = await screen.findByRole('button', { name: 'Sign in with your passkey' })
    expect(button).toBeEnabled()
    expect(document.querySelectorAll('input')).toHaveLength(0)
    expect(screen.getByTestId('login-help')).not.toHaveAttribute('open')
    expect(screen.getByTestId('login-for-it')).toHaveTextContent('ssh -o ExitOnForwardFailure=yes -L 47821:127.0.0.1:47821')
    expect(screen.getByTestId('login-for-it')).toHaveTextContent('orch dashboard open --print-url')
  })
  it('names the IT contact in the help only when one is given', async () => {
    const { unmount } = render(<LoginGate local={LOCAL} />)
    await screen.findByRole('button', { name: /passkey/ })
    expect(screen.getByTestId('login-help')).toHaveTextContent('your IT contact')
    unmount()
    render(<LoginGate local={LOCAL} contact="Sam at the helpdesk" />)
    await screen.findByRole('button', { name: /passkey/ })
    expect(screen.getByTestId('login-help')).toHaveTextContent('Ask Sam at the helpdesk')
  })
  it('waits for the device, and a failure says nothing changed and offers Try again with the help open', async () => {
    let fail!: (e: unknown) => void
    m('signIn').mockReturnValue(new Promise((_, rej) => { fail = rej }))
    render(<LoginGate local={LOCAL} />)
    await userEvent.click(await screen.findByRole('button', { name: /Sign in with your passkey/ }))
    expect(screen.getByRole('button', { name: /Confirm on your device/ })).toBeDisabled()
    await act(async () => fail(new PasskeyError('failed')))
    expect(await screen.findByText('That did not work')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
    expect(screen.getByTestId('login-help')).toHaveAttribute('open')
  })
  it('a cancelled prompt is calm', async () => {
    m('signIn').mockRejectedValue(new PasskeyError('cancelled'))
    render(<LoginGate local={LOCAL} />)
    await userEvent.click(await screen.findByRole('button', { name: /Sign in with your passkey/ }))
    expect(await screen.findByText('You cancelled')).toBeInTheDocument()
  })
  it('a lockout disables the button with the time, and enables it by itself', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    m('signIn').mockRejectedValue(new PasskeyError('locked'))
    render(<LoginGate local={LOCAL} />)
    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(await screen.findByRole('button', { name: /Sign in with your passkey/ }))
    expect(await screen.findByText('Too many tries')).toBeInTheDocument()
    expect(screen.getByText(/It opens again at/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sign in with your passkey/ })).toBeDisabled()
    await act(async () => { await vi.advanceTimersByTimeAsync(61_000) })
    expect(screen.getByRole('button', { name: /Sign in with your passkey/ })).toBeEnabled()
  })
  it('says so when the session ended, and when orch does not answer', async () => {
    const { unmount } = render(<LoginGate ended local={LOCAL} />)
    expect(await screen.findByText('You were signed out')).toBeInTheDocument()
    unmount()
    m('prepareLogin').mockRejectedValue(new PasskeyError('network'))
    render(<LoginGate local={LOCAL} />)
    expect(await screen.findByText('orch did not answer')).toBeInTheDocument()
  })
  it('a browser without passkeys says so instead of a dead button', async () => {
    m('passkeysSupported').mockReturnValue(false)
    render(<LoginGate local={LOCAL} />)
    expect(await screen.findByText('This browser cannot use a passkey')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Sign in/ })).toBeNull()
  })
})

describe('the tunnel line', () => {
  it('names the port the dashboard server uses on a headless machine', async () => {
    const { readFileSync } = await import('node:fs')
    const server = readFileSync(`${process.cwd()}/server/src/orch_dashboard/__main__.py`, 'utf8')
    expect(/^HEADLESS_PORT = (\d+)/m.exec(server)?.[1]).toBe(String(passkey.HEADLESS_PORT))
  })
})

describe('add this device', () => {
  beforeEach(() => { history.replaceState(null, '', '/#enroll=' + 'a'.repeat(30)) })
  const open = async () => {
    const { bootstrap } = await import('@/api/bootstrap')
    await bootstrap(vi.fn())
  }
  it('one card, one button, a device name set for the person', async () => {
    await open()
    render(<EnrollPage />)
    expect(screen.getByRole('heading', { name: 'Add this device' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add this device' })).toBeEnabled()
    expect(document.querySelectorAll('input')).toHaveLength(0)
    expect(location.hash).toContain('enroll=') // kept so a reload works until the link is used
  })
  it('waits, then succeeds and opens orch on the click; the link leaves the address bar', async () => {
    await open()
    let done!: (t: string) => void
    m('enrollDevice').mockReturnValue(new Promise((r) => { done = r }))
    const onDone = vi.fn()
    render(<EnrollPage onDone={onDone} />)
    await userEvent.click(screen.getByRole('button', { name: 'Add this device' }))
    expect(screen.getByRole('heading', { name: 'Confirm on this device' })).toBeInTheDocument()
    await act(async () => done('bearer'))
    expect(await screen.findByRole('heading', { name: 'This device is ready' })).toBeInTheDocument()
    expect(location.hash).toBe('')
    await userEvent.click(screen.getByRole('button', { name: 'Open orch' }))
    expect(onDone).toHaveBeenCalled()
  })
  it('a used or expired link says so and goes to sign in', async () => {
    await open()
    m('enrollDevice').mockRejectedValue(new PasskeyError('expired'))
    const onDone = vi.fn()
    render(<EnrollPage onDone={onDone} />)
    await userEvent.click(screen.getByRole('button', { name: 'Add this device' }))
    expect(await screen.findByRole('heading', { name: 'This link no longer works' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Go to sign in' }))
    expect(onDone).toHaveBeenCalled()
  })
  it('a device without passkeys: reload and copy details for IT', async () => {
    await open()
    m('passkeysSupported').mockReturnValue(false)
    render(<EnrollPage />)
    expect(screen.getByRole('heading', { name: 'This device cannot make a passkey' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload this page' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy details for IT' })).toBeInTheDocument()
  })
  it('a cancelled prompt can be tried again', async () => {
    await open()
    m('enrollDevice').mockRejectedValue(new PasskeyError('cancelled'))
    render(<EnrollPage />)
    await userEvent.click(screen.getByRole('button', { name: 'Add this device' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('You cancelled. Nothing was changed.'))
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
  })
})
