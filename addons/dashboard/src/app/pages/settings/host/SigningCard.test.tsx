// "Sign in the dashboard" (section 0.8.6 item 3a): the owner's switch. The server and the signer are faked: what is
// sent, what is shown, and that the passphrase is never kept in the form after a refusal.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { ClickCard } from '@/api/click'
import { ApiError, type Workspace } from '@/api/types'
import { SigningCard } from './SigningCard'

const WS = 'a'.repeat(32)
const workspace = { id: WS, prefix: 'INT', name: 'orchestrator', click: null } as unknown as Workspace
const OFF: ClickCard = { custody: 'passphrase', enabled: false, available: false, problem: 'not_convenience', switchable: true }
const ON: ClickCard = { custody: 'convenience', enabled: true, available: true, switchable: true }

function mount(card: ClickCard, owner = true) {
  vi.spyOn(api, 'clickStatus').mockResolvedValue(card)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SigningCard workspace={workspace} owner={owner} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('Sign in the dashboard', () => {
  it('shows the risk line and Off, and switches on with the passphrase', async () => {
    const sw = vi.spyOn(api, 'switchClick').mockResolvedValue(ON)
    mount(OFF)
    expect(await screen.findByTestId('signing-state')).toHaveTextContent(/^Off/)
    expect(screen.getByTestId('click-risk')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Turn on…' }))
    const field = screen.getByLabelText(/Your passphrase/)
    expect(field).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Turn on' })).toBeDisabled()
    fireEvent.change(field, { target: { value: 'my passphrase' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent(/^On: actions are signed here/))
    expect(sw).toHaveBeenCalledWith(WS, { on: true, passphrase: 'my passphrase' })
    expect(screen.queryByLabelText(/Your passphrase/)).toBeNull()
  })

  it('a wrong passphrase says so, keeps it off and clears the field', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(403, { code: 'custody.wrong_passphrase', message: 'x', retryable: false }))
    mount(OFF)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn on…' }))
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByTestId('signing-error')).toHaveTextContent('That passphrase was refused. Nothing changed.')
    expect(screen.getByLabelText(/Your passphrase/)).toHaveValue('')
    expect(screen.getByTestId('signing-state')).toHaveTextContent(/^Off/)
  })

  it('too many wrong passphrases name the wait', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(429, { code: 'rate.limited', message: 'x', retryable: true, retry_after_ms: 600_000 }))
    mount(OFF)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn on…' }))
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByTestId('signing-error')).toHaveTextContent('Try again in 10 min')
  })

  it('turns off without a passphrase, keeping the key in the key store', async () => {
    const sw = vi.spyOn(api, 'switchClick').mockResolvedValue({ ...OFF, custody: 'convenience', problem: 'disabled' })
    mount(ON)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }))
    expect(screen.getByText(/Your key stays in this computer's key store/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(sw).toHaveBeenCalledWith(WS, { on: false }))
    await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent(/^Off/))
  })

  it('turning off never offers a new passphrase: moving the key back is a terminal command', async () => {
    mount(ON)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn off' }))
    expect(screen.queryByLabelText(/new passphrase/i)).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(document.querySelectorAll('input[type=password]')).toHaveLength(0)
    expect(screen.getByTestId('signing-move-back')).toHaveTextContent('orch keys custody passphrase --workspace INT --in-place')
  })

  it('a key already held with signing off is switched on in the terminal, never with a passphrase here', async () => {
    const sw = vi.spyOn(api, 'switchClick')
    mount({ custody: 'convenience', enabled: false, available: false, problem: 'disabled', switchable: true })
    expect(await screen.findByTestId('signing-terminal-on')).toHaveTextContent('orch host convenience on --workspace INT')
    expect(screen.queryByRole('button', { name: /Turn on/ })).toBeNull()
    expect(document.querySelectorAll('input[type=password]')).toHaveLength(0)
    expect(sw).not.toHaveBeenCalled()
  })

  it('the on form shows what moves where, and the field is not autofilled', async () => {
    mount({ ...OFF, device: 'd_' + '1'.repeat(32) })
    fireEvent.click(await screen.findByRole('button', { name: 'Turn on…' }))
    const details = screen.getByTestId('signing-conversion')
    expect(details).toHaveTextContent('orchestrator (INT)')
    expect(details).toHaveTextContent('d_' + '1'.repeat(32))
    expect(details).toHaveTextContent(/moves to.*this computer's key store/i)
    expect(screen.getByLabelText(/Your passphrase/)).toHaveAttribute('autocomplete', 'off')
  })

  it('the signer asking for the terminal names the command', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(409, { code: 'custody.terminal_needed', message: 'x', retryable: false }))
    mount(OFF)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn on…' }))
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByTestId('signing-error')).toHaveTextContent('orch host convenience on --workspace INT')
  })

  it('only an owner sees the switch', async () => {
    mount(OFF, false)
    expect(await screen.findByText('Only an owner switches this.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Turn/ })).toBeNull()
  })

  it('an older signer names the terminal command instead', async () => {
    mount({ ...OFF, switchable: false })
    expect(await screen.findByTestId('signing-update')).toHaveTextContent('orch host convenience on --workspace INT')
    expect(screen.queryByRole('button', { name: /Turn/ })).toBeNull()
  })
})
