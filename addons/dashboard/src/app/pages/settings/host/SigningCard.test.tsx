// "Sign in the dashboard" (section 0.8.6 item 3a): the owner's switch. The server and the signer are faked: what is
// sent, what is shown, and that the passphrase is never kept in the form after a refusal.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { ClickCard } from '@/api/click'
import { ApiError, type Workspace } from '@/api/types'
import { SigningCard } from './SigningCard'

const WS = 'a'.repeat(32)
const workspace = { id: WS, prefix: 'INT', name: 'orchestrator', click: null } as unknown as Workspace
const HOSTILE = { ...workspace, prefix: 'X;rm -rf ~' } as unknown as Workspace
const OFF: ClickCard = { custody: 'passphrase', enabled: false, available: false, problem: 'not_convenience', switchable: true }
const ON: ClickCard = { custody: 'convenience', enabled: true, available: true, switchable: true }

let qcOf: QueryClient
function mount(card: ClickCard, owner = true, ws: Workspace = workspace) {
  vi.spyOn(api, 'clickStatus').mockResolvedValue(card)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qcOf = qc
  return render(
    <QueryClientProvider client={qc}>
      <SigningCard workspace={ws} owner={owner} />
    </QueryClientProvider>,
  )
}

/** The One click switch, once the signer's answer has arrived (it is disabled until then). */
async function openSwitch() {
  const sw = await screen.findByRole('switch', { name: /one click/i })
  await waitFor(() => expect(sw).toBeEnabled())
  return sw
}

async function typeAndSubmit(pw = 'pw') {
  fireEvent.click(await openSwitch())
  fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: pw } })
  fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
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
    fireEvent.click(await openSwitch())
    const field = screen.getByLabelText(/Your passphrase/)
    expect(field).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Turn on' })).toBeDisabled()
    fireEvent.change(field, { target: { value: 'my passphrase' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent(/^On: you approve here with one click/))
    expect(sw).toHaveBeenCalledWith(WS, { on: true, passphrase: 'my passphrase' })
    expect(screen.queryByLabelText(/Your passphrase/)).toBeNull()
  })

  it('a wrong passphrase says so, keeps it off and clears the field', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(403, { code: 'custody.wrong_passphrase', message: 'x', retryable: false }))
    mount(OFF)
    fireEvent.click(await openSwitch())
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByTestId('signing-error')).toHaveTextContent('That passphrase was refused. Nothing changed.')
    expect(screen.getByLabelText(/Your passphrase/)).toHaveValue('')
    expect(screen.getByTestId('signing-state')).toHaveTextContent(/^Off/)
  })

  it('a dev sandbox host refusing the switch says why, and an unknown reason stays generic', async () => {
    const sw = vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(409, { code: 'host.dev_refused', message: 'x', retryable: false, detail: { reason: 'dev_host' } }))
    mount(OFF)
    await typeAndSubmit()
    expect(await screen.findByTestId('signing-error')).toHaveTextContent(/off on a dev sandbox host/)
    sw.mockRejectedValue(new ApiError(409, { code: 'host.dev_refused', message: 'x', retryable: false, detail: { reason: 'brand_new_reason' } }))
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(screen.getByTestId('signing-error')).not.toHaveTextContent(/brand_new/))
  })

  it('too many wrong passphrases name the wait', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(429, { code: 'rate.limited', message: 'x', retryable: true, retry_after_ms: 600_000 }))
    mount(OFF)
    fireEvent.click(await openSwitch())
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'wrong' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByTestId('signing-error')).toHaveTextContent('Try again in 10 min')
  })

  it('turning off asks for a new passphrase twice and moves the key back under it (V3-D31)', async () => {
    const sw = vi.spyOn(api, 'switchClick').mockResolvedValue({ ...OFF, custody: 'passphrase' })
    mount(ON)
    fireEvent.click(await openSwitch())
    expect(screen.getByTestId('signing-off-explain')).toHaveTextContent(/protect your key with a new passphrase/)
    expect(screen.getByTestId('signing-off-copies')).toHaveTextContent(/backups or snapshots/)
    const one = screen.getByLabelText(/^New passphrase \(at least 14/)
    const two = screen.getByLabelText('New passphrase again')
    expect(one).toHaveAttribute('type', 'password')
    const submit = () => screen.getByRole('button', { name: 'Turn off' })
    expect(submit()).toBeDisabled()
    fireEvent.change(one, { target: { value: 'short' } })
    fireEvent.change(two, { target: { value: 'short' } })
    expect(submit()).toBeDisabled() // under 14 characters
    fireEvent.change(one, { target: { value: 'violet harbour 7 kettles' } })
    fireEvent.change(two, { target: { value: 'violet harbour 7 kettle' } })
    expect(screen.getByTestId('signing-mismatch')).toBeInTheDocument()
    expect(submit()).toBeDisabled()
    fireEvent.change(two, { target: { value: 'violet harbour 7 kettles' } })
    fireEvent.click(submit())
    await waitFor(() => expect(sw).toHaveBeenCalledWith(WS, { on: false, passphrase: 'violet harbour 7 kettles' }))
    await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent(/^Off/))
    expect(screen.queryByLabelText('New passphrase again')).toBeNull()
  })

  it('a too-weak new passphrase is reported by core, the fields are cleared and signing is still on', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(409, { code: 'custody.unavailable', message: 'that passphrase is on the list of common ones', retryable: false }))
    mount(ON)
    fireEvent.click(await openSwitch())
    fireEvent.change(screen.getByLabelText(/^New passphrase \(at least/), { target: { value: 'password123456' } })
    fireEvent.change(screen.getByLabelText('New passphrase again'), { target: { value: 'password123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    expect(await screen.findByTestId('signing-error')).toHaveTextContent(/common ones. Nothing changed: one-click signing is still on/)
    expect(screen.getByLabelText(/^New passphrase \(at least/)).toHaveValue('')
  })

  it('with the key already in passphrase custody, turning off is the flag only', async () => {
    const sw = vi.spyOn(api, 'switchClick').mockResolvedValue(OFF)
    mount({ ...ON, custody: 'passphrase' })
    fireEvent.click(await openSwitch())
    expect(document.querySelectorAll('input[type=password]')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(sw).toHaveBeenCalledWith(WS, { on: false }))
  })

  it('a key already held with signing off is switched on in the terminal, never with a passphrase here', async () => {
    const sw = vi.spyOn(api, 'switchClick')
    mount({ custody: 'convenience', enabled: false, available: false, problem: 'disabled', switchable: true })
    expect(await screen.findByTestId('signing-terminal-on')).toHaveTextContent('orch host convenience on --workspace INT')
    expect(screen.queryByRole('button', { name: /Turn on/ })).toBeNull()
    expect(document.querySelectorAll('input[type=password]')).toHaveLength(0)
    expect(sw).not.toHaveBeenCalled()
  })

  it('says in plain words that One click switched itself off after idle hours', async () => {
    mount({ custody: 'convenience', enabled: false, available: false, problem: 'idle', idle_hours: 12, switchable: true })
    expect(await screen.findByTestId('signing-idle-off')).toHaveTextContent('One click switched itself off after 12 hours without use. Turn it on again to continue.')
    expect(screen.getByTestId('signing-state')).toHaveTextContent('Off: you approve in the terminal')
  })

  it('turns One click on again with a plain click after the review and the risk notice, no passphrase', async () => {
    const sw = vi.spyOn(api, 'switchClick').mockResolvedValue(ON)
    mount({ custody: 'convenience', enabled: false, available: false, problem: 'idle', idle_hours: 12, switchable: true })
    fireEvent.click(await openSwitch())
    expect(screen.getByTestId('signing-resume')).toHaveTextContent('switches itself off again after 12 hours')
    expect(screen.getByTestId('resume-risk')).toHaveTextContent(/agents running as you/)
    expect(document.querySelectorAll('input[type=password]')).toHaveLength(0)
    expect(sw).not.toHaveBeenCalled() // nothing before the click on Turn on
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(sw).toHaveBeenCalledWith(WS, { on: true, resume: true }))
    await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent(/^On: you approve here/))
  })

  it('a refused resume (not a dashboard on this computer) keeps the terminal command', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(403, { code: 'custody.terminal_needed', message: 'needs this computer', retryable: false }))
    mount({ custody: 'convenience', enabled: false, available: false, problem: 'idle', idle_hours: 12, switchable: true })
    fireEvent.click(await openSwitch())
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect(await screen.findByTestId('signing-error')).toHaveTextContent('orch host convenience on --workspace INT')
  })

  it('the on form shows what moves where, and the field asks for the current passphrase', async () => {
    mount({ ...OFF, device: 'd_' + '1'.repeat(32) })
    fireEvent.click(await openSwitch())
    const details = screen.getByTestId('signing-conversion')
    expect(details).toHaveTextContent('orchestrator (INT)')
    expect(details).toHaveTextContent('d_' + '1'.repeat(32))
    expect(details).toHaveTextContent(/moves from passphrase protection to this computer's saved keys/i)
    // an existing passphrase is proven: browsers ignore autocomplete=off on password fields (orch-9d L2)
    expect(screen.getByLabelText(/Your passphrase/)).toHaveAttribute('autocomplete', 'current-password')
  })

  it('the signer asking for the terminal names the command', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(409, { code: 'custody.terminal_needed', message: 'x', retryable: false }))
    mount(OFF)
    fireEvent.click(await openSwitch())
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

  // -- orch-9d's review of #578 (M1, M2, L1-L5) -----------------------------------------------------------------------

  it('a hostile prefix is never put into a command (M1)', async () => {
    mount({ ...ON, custody: 'passphrase' }, true, HOSTILE)
    fireEvent.click(await openSwitch())
    expect(screen.getByTestId('signing-move-back')).toHaveTextContent(/can't be put in a command safely/)
    for (const code of document.querySelectorAll('code')) expect(code.textContent).not.toContain('rm -rf')
    expect(screen.queryByRole('button', { name: /Copy/ })).toBeNull()
  })

  it('a hostile prefix gets no terminal-on command either (M1)', async () => {
    mount({ custody: 'convenience', enabled: false, available: false, problem: 'disabled', switchable: true }, true, HOSTILE)
    expect(await screen.findByTestId('signing-terminal-on')).toHaveTextContent(/can't be put in a command safely/)
    for (const code of document.querySelectorAll('code')) expect(code.textContent).not.toContain('rm -rf')
  })

  it('the commands have a copy button (L1)', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    mount({ ...ON, custody: 'passphrase' })
    fireEvent.click(await openSwitch())
    fireEvent.click(screen.getByRole('button', { name: 'Copy the command' }))
    expect(writeText).toHaveBeenCalledWith('orch keys custody passphrase --workspace INT --in-place')
  })

  it('every error reloads the card: a stale form gives way to what the signer says now (M2)', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(409, { code: 'custody.terminal_needed', message: 'x', retryable: false }))
    mount(OFF)
    const status = vi.mocked(api.clickStatus)
    fireEvent.click(await openSwitch())
    await waitFor(() => expect(status).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'pw' } })
    // the signer now says the key is already in the key store (the reload after the refusal reads this)
    status.mockResolvedValue({ custody: 'convenience', enabled: false, available: false, problem: 'disabled', switchable: true })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(status.mock.calls.length).toBeGreaterThanOrEqual(2))
    // the refetched state replaces the stale form: the terminal-on view, no form, no error
    expect(await screen.findByTestId('signing-terminal-on')).toHaveTextContent('orch host convenience on --workspace INT')
    expect(screen.queryByLabelText(/Your passphrase/)).toBeNull()
    expect(screen.queryByTestId('signing-error')).toBeNull()
  })

  it('after "result unknown", the form closes once the reloaded card differs, and keeps no passphrase (#580)', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new TypeError('fetch failed'))
    mount(OFF)
    const status = vi.mocked(api.clickStatus)
    await typeAndSubmit('never kept')
    expect(await screen.findByTestId('signing-error')).toHaveTextContent(/Reloading the card/)
    // the reload found the same card: the form stays, with its message
    await waitFor(() => expect(status.mock.calls.length).toBeGreaterThanOrEqual(2))
    expect(screen.getByLabelText(/Your passphrase/)).toHaveValue('')
    expect(screen.getByTestId('signing-error')).toBeInTheDocument()
    // a later reload shows the switch did happen
    status.mockResolvedValue(ON)
    await act(async () => {
      await qcOf.invalidateQueries({ queryKey: ['click-status', WS] })
    })
    await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent(/^On: you approve here with one click/))
    expect(screen.queryByLabelText(/Your passphrase/)).toBeNull()
    expect(screen.queryByTestId('signing-error')).toBeNull()
    expect(screen.queryByText(/Reloading the card/)).toBeNull()
    expect(document.body.innerHTML).not.toContain('never kept')
  })

  it('a timeout does not claim nothing changed (M2)', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(503, { code: 'click.unavailable', message: 'The signing helper did not answer in time. Try again.', retryable: false }))
    mount(OFF)
    const status = vi.mocked(api.clickStatus)
    await typeAndSubmit()
    const err = await screen.findByTestId('signing-error')
    expect(err).toHaveTextContent(/result is unknown/i)
    expect(err).not.toHaveTextContent(/nothing changed/i)
    await waitFor(() => expect(status.mock.calls.length).toBeGreaterThanOrEqual(2))
  })

  it('an unknown code or a lost connection is also "unknown", never "nothing changed" (M2)', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValueOnce(new ApiError(500, { code: 'internal', message: 'boom', retryable: false })).mockRejectedValueOnce(new TypeError('fetch failed'))
    mount(OFF)
    await typeAndSubmit()
    expect(await screen.findByTestId('signing-error')).toHaveTextContent(/result is unknown/i)
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'pw' } })
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    await waitFor(() => expect(screen.getByTestId('signing-error')).toHaveTextContent(/result is unknown/i))
  })

  it('the error is tied to the field, and the field is cleared after it (L4, L5)', async () => {
    vi.spyOn(api, 'switchClick').mockRejectedValue(new ApiError(403, { code: 'custody.wrong_passphrase', message: 'x', retryable: false }))
    mount(OFF)
    await typeAndSubmit('secret words')
    const err = await screen.findByTestId('signing-error')
    const field = screen.getByLabelText(/Your passphrase/)
    expect(field).toHaveValue('')
    expect(field.getAttribute('aria-describedby')).toContain(err.id)
    expect(err.id).not.toBe('')
  })

  it('a double submit sends once (L4)', async () => {
    let settle: (c: ClickCard) => void = () => {}
    const sw = vi.spyOn(api, 'switchClick').mockImplementation(() => new Promise((r) => (settle = r)))
    mount(OFF)
    fireEvent.click(await openSwitch())
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'pw' } })
    const form = screen.getByLabelText(/Your passphrase/).closest('form')!
    // Both submits arrive inside one act(): React has not re-rendered in between, so `busy` is still false for the
    // second one. Only the ref guard stops it (without the guard this sends twice while `busy` still works).
    act(() => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    })
    expect(sw).toHaveBeenCalledTimes(1)
    // and once `busy` has rendered, the controls are off and a further submit still sends nothing
    expect(screen.getByLabelText(/Your passphrase/)).toBeDisabled()
    fireEvent.submit(form)
    expect(sw).toHaveBeenCalledTimes(1)
    settle(ON)
    await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent(/^On/))
  })

  it('the passphrase does not outlive the card (L4)', async () => {
    const first = mount(OFF)
    fireEvent.click(await openSwitch())
    fireEvent.change(screen.getByLabelText(/Your passphrase/), { target: { value: 'typed then left' } })
    first.unmount()
    mount(OFF)
    fireEvent.click(await openSwitch())
    expect(screen.getByLabelText(/Your passphrase/)).toHaveValue('')
    expect(document.body.innerHTML).not.toContain('typed then left')
  })
})

it('says the risk in one plain line and keeps the full notice under Details', async () => {
  mount({ ...ON, risk_ack: null })
  expect(await screen.findByTestId('click-risk')).toHaveTextContent('anyone using this browser session, and agents running as you, could approve')
  expect(screen.getByTestId('click-risk-full')).toHaveTextContent('Convenience signing:')
  await waitFor(() => expect(screen.getByTestId('signing-state')).toHaveTextContent('On: you approve here with one click'))
})

it('shows the ISO risk acknowledgement as a date in Settings', async () => {
  const at = '2026-10-16T18:30:00Z'
  mount({ ...ON, risk_ack: at })
  const notice = await screen.findByText(/Risk notice acknowledged on/)
  expect(notice.querySelector('time')).toHaveAttribute('datetime', at)
  expect(notice).toHaveTextContent('16 Oct 18:30') // local zone (UTC in tests), the shared formatter
})
