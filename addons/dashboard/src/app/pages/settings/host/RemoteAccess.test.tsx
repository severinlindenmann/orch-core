import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setFeatures } from '@/api/features'
import * as passkey from '@/api/passkey'
import { PasskeyError } from '@/api/passkey'
import { RemoteAccess } from './RemoteAccess'

vi.mock('@/api/passkey', async (orig) => ({ ...(await orig<typeof import('@/api/passkey')>()), listPasskeys: vi.fn(), createEnrollLink: vi.fn(), removePasskey: vi.fn(), logoutEverywhere: vi.fn() }))
const m = <T extends keyof typeof passkey>(k: T) => passkey[k] as unknown as ReturnType<typeof vi.fn>
const row = (id: string, name: string, last: string | null = null) => ({ id, name, created: '2026-10-03T10:00:00Z', last_used: last })
const mac = row('mac', 'Safari on Mac', new Date().toISOString())
const phone = row('phone', 'Safari on iPhone', '2026-10-24T10:00:00Z')

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><RemoteAccess /></QueryClientProvider>)
}
beforeEach(() => {
  vi.clearAllMocks()
  setFeatures({ features: [], login: 'remote', remote_address: 'https://orch.example.com' })
  m('listPasskeys').mockResolvedValue([mac, phone])
})
afterEach(() => { vi.useRealTimers(); setFeatures(null) })

describe('Remote access settings', () => {
  it('shows the address, the passkeys with last used, and Log out everywhere', async () => {
    mount()
    expect(screen.getByTestId('remote-address')).toHaveTextContent('https://orch.example.com')
    expect(await screen.findAllByTestId('passkey-row')).toHaveLength(2)
    expect(screen.getByText('Just now')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Log out everywhere' })).toBeInTheDocument()
  })
  it('never offers Remove for the only passkey and says why in words', async () => {
    m('listPasskeys').mockResolvedValue([mac])
    mount()
    expect(await screen.findByTestId('only-passkey')).toHaveTextContent('Safari on Mac is your only passkey')
    expect(screen.queryByRole('button', { name: 'Remove' })).toBeNull()
  })
  it('Remove shows what goes and what stays before the touch', async () => {
    mount()
    await userEvent.click((await screen.findAllByRole('button', { name: 'Remove' }))[1])
    const review = screen.getByTestId('remove-review')
    expect(review).toHaveTextContent('Removed: passkey "Safari on iPhone". Still able to sign in: "Safari on Mac"')
    m('removePasskey').mockResolvedValue(undefined)
    await userEvent.click(within(review).getByRole('button', { name: 'Touch your passkey to remove' }))
    expect(m('removePasskey')).toHaveBeenCalledWith('phone')
    await waitFor(() => expect(screen.queryByTestId('remove-review')).toBeNull())
  })
  it('Keep it closes the review and touches nothing', async () => {
    mount()
    await userEvent.click((await screen.findAllByRole('button', { name: 'Remove' }))[0])
    await userEvent.click(screen.getByRole('button', { name: 'Keep it' }))
    expect(screen.queryByTestId('remove-review')).toBeNull()
    expect(m('removePasskey')).not.toHaveBeenCalled()
  })
  it('Add a device warns first, then shows link, QR and expiry; a new link on expiry', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    m('createEnrollLink').mockResolvedValue({ link: 'https://orch.example.com/#enroll=abc', expiresInS: 30 })
    mount()
    expect(screen.getByText(/Whoever opens the link below can add a device that signs in as you/)).toBeInTheDocument()
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(await screen.findByRole('button', { name: /Add a device/ }))
    const box = await screen.findByTestId('enroll-link')
    expect(box).toHaveTextContent('https://orch.example.com/#enroll=abc')
    expect(within(box).getByRole('img', { name: 'Link as a QR code' })).toBeInTheDocument()
    expect(screen.getByTestId('link-expiry')).toHaveTextContent('Expires in 0 min')
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
    expect(await screen.findByTestId('link-expired')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Create a new link/ })).toBeEnabled()
  })
  it('says Link used when a new passkey appears', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    m('createEnrollLink').mockResolvedValue({ link: 'https://orch.example.com/#enroll=abc', expiresInS: 900 })
    mount()
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    await user.click(await screen.findByRole('button', { name: /Add a device/ }))
    await screen.findByTestId('enroll-link')
    m('listPasskeys').mockResolvedValue([mac, phone, row('new', 'Chrome on Windows')])
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(await screen.findByTestId('link-used')).toBeInTheDocument()
  })
  it('a cancelled touch for Add a device leaves the card as it was', async () => {
    m('createEnrollLink').mockRejectedValue(new PasskeyError('cancelled'))
    mount()
    await userEvent.click(await screen.findByRole('button', { name: /Add a device/ }))
    expect(screen.queryByTestId('enroll-link')).toBeNull()
  })
  it('Log out everywhere calls the server', async () => {
    m('logoutEverywhere').mockResolvedValue(undefined)
    mount()
    await userEvent.click(screen.getByRole('button', { name: 'Log out everywhere' }))
    expect(m('logoutEverywhere')).toHaveBeenCalled()
  })
})

describe('Remote access problem', () => {
  it('says people cannot sign in, copies details for IT, and still shows no passkey table', async () => {
    setFeatures({ features: [], login: 'local', remote_problem: 'remote.not_configured: the front socket did not start' })
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    mount()
    expect(screen.getByTestId('remote-problem')).toHaveTextContent('People cannot sign in from the web right now')
    expect(screen.queryByTestId('passkey-row')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Copy details for IT' }))
    expect(writeText.mock.calls[0][0]).toContain('remote.not_configured: the front socket did not start')
    expect(m('listPasskeys')).not.toHaveBeenCalled()
  })
})
