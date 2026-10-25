// #1198 C13: "Replace this device's key". The server and the signer are faked at the api boundary; the seal is real.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ROTATE_LINE, type RotateOffer } from '@/api/click'
import * as sealed from '@/api/sealed'
import { ApiError, type Workspace } from '@/api/types'
import { ReplaceKey } from './ReplaceKey'

const WS = 'a'.repeat(32)
const workspace = { id: WS, prefix: 'INT', name: 'orchestrator', click: null } as unknown as Workspace
const REQ = 'hr_01M5PVRB46WS3X8H5X2RQK04S9'
const DIGEST = 'sha256:' + 'a'.repeat(64)
const OLD = 'd_' + '1'.repeat(32)
const NEW = 'd_' + '2'.repeat(32)
const WORDS = 'orchid anchor ribbon velvet quarter timber lantern cobalt meadow harbor window thunder'
const PW = 'violet harbour 7 kettles juggle quietly!'
const T = { timeout: 5000 }

let pk = ''
const offer = (over: Partial<RotateOffer['view']> = {}): RotateOffer => ({
  request_id: REQ,
  view_digest: DIGEST,
  expires_in: 300,
  view: {
    op: 'device.rotate', workspace: { id: WS, name: 'orchestrator', prefix: 'INT' }, old: OLD, new: NEW,
    lines: ['Replace the signing key of this computer for orchestrator.', 'The old key stops signing everywhere.', `old device: ${OLD}`, `new device: ${NEW}`, ROTATE_LINE],
    confirm: { kind: 'click' }, rotate_line: ROTATE_LINE,
    seal: { v: 1, alg: sealed.ALG, pk, purposes: ['device.recovery_code', 'custody.passphrase'] }, ...over,
  },
})
let begin: ReturnType<typeof vi.spyOn>
let send: ReturnType<typeof vi.spyOn>
let seal: ReturnType<typeof vi.spyOn>

beforeEach(async () => {
  const pair = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])
  pk = sealed.b64urlEncode(new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey)))
  begin = vi.spyOn(api, 'rotateSeal').mockImplementation(async () => offer() as never)
  send = vi.spyOn(api, 'rotateKey').mockResolvedValue({ done: true, custody: 'passphrase', enabled: false, available: false } as never)
  seal = vi.spyOn(sealed, 'seal')
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function mount(owner = true) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ReplaceKey workspace={workspace} owner={owner} /></QueryClientProvider>)
}
const field = async (id: string) => (await screen.findByTestId(id, {}, T)) as HTMLInputElement
async function open() {
  fireEvent.click(screen.getByTestId('replace-key-start'))
  return field('replace-key-code')
}
async function fill(words = WORDS, pw = PW, again = pw) {
  const c = await field('replace-key-code')
  fireEvent.change(c, { target: { value: words } })
  fireEvent.change(await field('replace-key-pw'), { target: { value: pw } })
  fireEvent.change(await field('replace-key-pw2'), { target: { value: again } })
}

describe('Replace this device’s key', () => {
  it('shows nothing to a non-owner', () => {
    mount(false)
    expect(screen.queryByTestId('replace-key')).toBeNull()
  })

  it('shows the review with password fields and the technical details folded', async () => {
    mount()
    const code = await open()
    expect(code.type).toBe('password')
    expect(screen.getByText(/Replace the signing key of this computer/)).toBeInTheDocument()
    expect(screen.getByText('Technical details').closest('details')).not.toHaveAttribute('open')
    // every review line is on the page (the touch binds the whole review): the rest sit in the fold
    expect(screen.getByTestId('replace-key-review')).toHaveTextContent(ROTATE_LINE)
    expect(screen.getByTestId('replace-key-review')).toHaveTextContent(`new device: ${NEW}`)
    expect(screen.getByTestId('replace-key-go')).toBeDisabled()
    expect(begin).toHaveBeenCalledWith(WS)
  })

  it('needs matching long passphrases and a code before it can be pressed', async () => {
    mount()
    await open()
    await fill(WORDS, PW, 'something else entirely')
    expect(screen.getByTestId('replace-key-go')).toBeDisabled()
    await fill(WORDS, 'short', 'short')
    expect(screen.getByTestId('replace-key-go')).toBeDisabled()
    await fill()
    await waitFor(() => expect(screen.getByTestId('replace-key-go')).toBeEnabled())
  })

  it('empties the fields at the click, seals both for this request and review, and sends ciphertext only', async () => {
    mount()
    const code = await open()
    await fill()
    await waitFor(() => expect(screen.getByTestId('replace-key-go')).toBeEnabled())
    fireEvent.click(screen.getByTestId('replace-key-go'))
    expect(code.value).toBe('') // cleared at once
    await waitFor(() => expect(screen.getByTestId('replace-key-done')).toBeInTheDocument(), T)
    const ctx = { workspace_id: WS, request_id: REQ, view_digest: DIGEST }
    expect(seal.mock.calls.map((c: unknown[]) => [c[2]])).toEqual([[{ purpose: 'device.recovery_code', ...ctx }], [{ purpose: 'custody.passphrase', ...ctx }]])
    const body = send.mock.calls[0][1] as { sealed: Record<string, unknown> }
    expect(Object.keys(body.sealed)).toEqual(['device.recovery_code', 'custody.passphrase'])
    expect(JSON.stringify(send.mock.calls)).not.toMatch(/orchid|violet/)
    expect(document.body.textContent).not.toMatch(/orchid|violet/)
  })

  it('starts again with empty fields and a new review when the signer refuses', async () => {
    send.mockRejectedValueOnce(new ApiError(409, { code: 'human.click_refused', message: 'that recovery code is not yours; nothing was signed and your old key is unchanged', retryable: false, reason: 'check_failed' } as never))
    mount()
    await open()
    await fill()
    await waitFor(() => expect(screen.getByTestId('replace-key-go')).toBeEnabled())
    await act(async () => { fireEvent.click(screen.getByTestId('replace-key-go')) })
    await waitFor(() => expect(begin).toHaveBeenCalledTimes(2), T)
    expect(await screen.findByTestId('replace-key-error', {}, T)).toHaveTextContent(/not yours/)
    expect(((await field('replace-key-code')) as HTMLInputElement).value).toBe('')
  })

  it('does not offer a review that is not what it should be', async () => {
    begin.mockResolvedValue(offer({ seal: { v: 1, alg: sealed.ALG, pk, purposes: ['custody.passphrase'] } }) as never)
    mount()
    fireEvent.click(screen.getByTestId('replace-key-start'))
    expect(await screen.findByTestId('replace-key-error', {}, T)).toBeInTheDocument()
    expect(screen.queryByTestId('replace-key-code')).toBeNull()
  })

  it('says why when One click is still on', async () => {
    begin.mockRejectedValue(new ApiError(400, { code: 'invalid.input', message: 'Turn One click off first; then replace the key of this computer.', retryable: false } as never))
    mount()
    fireEvent.click(screen.getByTestId('replace-key-start'))
    expect(await screen.findByTestId('replace-key-error', {}, T)).toHaveTextContent(/Turn One click off first/)
  })

  it('finishes an earlier run that only needed the install', async () => {
    begin.mockResolvedValue({ done: true, custody: 'passphrase', enabled: false, available: false } as never)
    mount()
    fireEvent.click(screen.getByTestId('replace-key-start'))
    expect(await screen.findByTestId('replace-key-done', {}, T)).toBeInTheDocument()
  })

  it('keeps the browser from offering to save the recovery code or passphrase', async () => {
    const { container } = mount()
    const c = await open()
    expect(container.querySelector('form')).toBeNull()
    for (const id of ['replace-key-code', 'replace-key-pw', 'replace-key-pw2']) {
      const el = await field(id)
      expect(el.getAttribute('autocomplete')).toBe('new-password')
      expect(el.hasAttribute('data-1p-ignore')).toBe(true)
      expect(el.getAttribute('data-lpignore')).toBe('true')
    }
    expect(c).toBeTruthy()
  })
})
