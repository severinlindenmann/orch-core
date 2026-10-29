// #1198 C10: a hub step by click. The server and the signer are faked at the api boundary; the seal is real (WebCrypto).
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, BACKUP_KEY_LINE, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import * as sealed from '@/api/sealed'
import { TooltipProvider } from '@/components/ui/tooltip'
import { trustedPresses } from '@/test/hostHarness'
import { HubStep } from './HubStep'

const WS = '705d40abbb8c1c90354a1acaa94c935c'
const REQ = 'hr_01M5PVRB46WS3X8H5X2RQK04S9'
const DIGEST = 'sha256:' + 'a'.repeat(64)
const WORDS = 'orchid anchor ribbon velvet quarter timber lantern cobalt meadow harbor window thunder'
const T = { timeout: 5000 }
const JOIN = { action: 'hub_join' as const, hub_card: '{"o":{}}', hub_id: 'a'.repeat(32) }

let pk = ''
const view = (over: Partial<ClickViewAnswer['view']> = {}): ClickViewAnswer => ({
  view_digest: DIGEST,
  view: {
    risk: CLICK_RISK, op: 'hub.join', workspace: { id: WS, name: 'billing', prefix: 'DEMO' }, fingerprint: '0123456789ab',
    lines: ['join the hub', '', BACKUP_KEY_LINE], checks: [], confirm: { kind: 'click' },
    expires_at: new Date(Date.now() + 600_000).toISOString(), auth: 'convenience', device: 'd_x', artifacts: null,
    seal: { v: 1, alg: 'P256-HKDF-SHA256-A256GCM', pk, purposes: ['hub.backup_key'] },
    backup_key_line: BACKUP_KEY_LINE, ...over,
  },
})

let s: Record<string, ReturnType<typeof vi.spyOn>>
let seal: ReturnType<typeof vi.spyOn>
beforeEach(async () => {
  const pair = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])
  pk = sealed.b64urlEncode(new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey)))
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  seal = vi.spyOn(sealed, 'seal')
  s = {
    prepare: vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'hub.join', confirm: 'click' }),
    view: vi.spyOn(api, 'viewClick').mockResolvedValue(view()),
    sign: vi.spyOn(api, 'signClick').mockResolvedValue({ state: 'submitted', duplicate: false }),
    cancel: vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: REQ, state: 'cancelled' }),
  }
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
})

function mount(action: Parameters<typeof HubStep>[0]['action'] = JOIN, list: Array<{ id: string }> = []) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.spyOn(api, 'getWorkspaces').mockResolvedValue(list as never)
  return render(<QueryClientProvider client={client}><TooltipProvider><HubStep ws={WS} action={action} onClose={vi.fn()} /></TooltipProvider></QueryClientProvider>)
}
const input = async () => (await screen.findByTestId('hub-backup-input', {}, T)) as HTMLInputElement
const signBtn = () => screen.getByRole('button', { name: 'Connect' })
async function press() {
  await waitFor(() => expect(signBtn()).toBeEnabled(), { timeout: ARM_MS + 3000 })
  trustedPresses() // the armed Sign signs only on a trusted press (fireEvent is untrusted)
  fireEvent.pointerDown(signBtn())
  fireEvent.click(signBtn())
}

describe('a hub step by click', () => {
  it('asks for the backup key in a password field', async () => {
    mount()
    const el = await input()
    expect(el.type).toBe('password')
    expect(screen.getByText(/Backup key \(from setup\)/)).toBeInTheDocument()
    expect(screen.queryByTestId('hub-backup-risk')).toBeNull()
    expect(screen.queryByTestId('hub-terminal')).toBeNull()
    expect(s.prepare.mock.calls[0][1]).toEqual(JOIN)
  })

  it('keeps Sign off until a key is typed', async () => {
    mount()
    const el = await input()
    await new Promise((r) => setTimeout(r, ARM_MS + 300))
    expect(signBtn()).toBeDisabled()
    fireEvent.change(el, { target: { value: WORDS } })
    await waitFor(() => expect(signBtn()).toBeEnabled(), { timeout: 3000 })
  })

  it('seals the typed words to the signer key for this request and review, sends only the envelope, and empties the field', async () => {
    mount()
    const el = await input()
    fireEvent.change(el, { target: { value: `  ${WORDS}  ` } })
    await press()
    await waitFor(() => expect(s.sign).toHaveBeenCalledTimes(1), T)
    expect(el.value).toBe('') // cleared at the click, not after the answer
    expect(seal).toHaveBeenCalledWith(pk, WORDS, { purpose: 'hub.backup_key', workspace_id: WS, request_id: REQ, view_digest: DIGEST })
    const [, rid, digest, revealKey, env] = s.sign.mock.calls[0] as unknown as unknown[]
    expect([rid, digest, revealKey]).toEqual([REQ, DIGEST, undefined])
    expect(Object.keys(env as object)).toEqual(['hub.backup_key'])
    const e = (env as Record<string, sealed.Envelope>)['hub.backup_key']
    expect(Object.keys(e).sort()).toEqual(['alg', 'ct', 'epk', 'v'])
    expect(JSON.stringify(s.sign.mock.calls)).not.toContain('orchid')
    expect(document.body.textContent).not.toContain('orchid')
  })

  it('empties the field when the signer refuses, and never keeps the words', async () => {
    const { ApiError } = await import('@/api/types')
    s.sign.mockRejectedValue(new ApiError(409, { code: 'human.click_refused', message: 'that backup key is not yours; nothing was signed', retryable: false, reason: 'check_failed' }))
    mount()
    const el = await input()
    fireEvent.change(el, { target: { value: WORDS } })
    await press()
    await waitFor(() => expect(s.sign).toHaveBeenCalledTimes(1), T)
    expect(el.value).toBe('')
    expect(document.body.textContent).not.toContain('orchid')
    expect(s.cancel).not.toHaveBeenCalled() // a refusal leaves the request open for a retry
  })

  it('empties the field and signs nothing when the words cannot be sealed', async () => {
    seal.mockRejectedValue(new Error('x'))
    mount()
    const el = await input()
    fireEvent.change(el, { target: { value: WORDS } })
    await press()
    expect(await screen.findByText(/could not be prepared/, {}, T)).toBeInTheDocument()
    expect(el.value).toBe('')
    expect(s.sign).not.toHaveBeenCalled()
  })

  it('offers no field for a review that lacks the line, the purpose or the signer key', async () => {
    for (const over of [{ seal: { v: 1, alg: 'x', purposes: ['custody.passphrase'], pk } }, { seal: { v: 1, alg: 'x', purposes: ['hub.backup_key'] } }]) {
      s.view.mockResolvedValue(view(over as Partial<ClickViewAnswer['view']>))
      mount()
      expect(await screen.findByTestId('hub-bad-view', {}, T)).toBeInTheDocument()
      expect(screen.queryByTestId('hub-backup-input')).toBeNull()
      cleanup()
    }
    expect(s.sign).not.toHaveBeenCalled()
  })

  it('folds exact signed values without changing the signing view', async () => {
    s.view.mockResolvedValue(view({ content: { headline: 'Hub', fields_changed: [{ field: 'hub', before: null, after: 'demo-hub-id' }] } }))
    mount()
    await input()
    expect(screen.getByText('demo-hub-id').closest('details')).not.toHaveAttribute('open')
    expect(screen.getByText('What this changes')).toBeTruthy()
  })

  it('a failed leave has a plain error, closed details, Cancel and a working retry', async () => {
    const { ApiError } = await import('@/api/types')
    s.prepare.mockRejectedValueOnce(new ApiError(409, { code: 'sign.convenience_unavailable', message: 'raw failure reason', retryable: false }))
    s.view.mockResolvedValue(view({ op: 'hub.leave' }))
    mount({ action: 'hub_leave' })
    expect(await screen.findByRole('alert', {}, T)).toHaveTextContent("Couldn't leave the hub. Nothing changed. Try again.")
    expect(screen.queryByText(/raw failure reason/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'More information' }))
    expect(await screen.findByText(/raw failure reason/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await input()
    expect(s.prepare).toHaveBeenCalledTimes(2)
  })

  it('discloses agent access ending only when the signed review says it ends', async () => {
    s.view.mockResolvedValue(view({ op: 'hub.mode', lines: ['Agent access on this computer ends.', BACKUP_KEY_LINE] }))
    mount({ action: 'hub_mode_full', hub_card: '{}', hub_id: 'demo-hub-id' })
    await input()
    expect(screen.getAllByText('Agent access on this computer ends.').some(el => !el.closest('details'))).toBe(true)
  })

  it('leave has its own button', async () => {
    s.view.mockResolvedValue(view({ op: 'hub.leave' }))
    mount({ action: 'hub_leave' })
    await input()
    expect(screen.getByRole('button', { name: 'Leave hub' })).toBeInTheDocument()
  })
})

describe('the sticky sign bar', () => {
  it('scrolls the dialog on focus so the field block ends above the bar', async () => {
    mount()
    const el = await input()
    const block = screen.getByTestId('hub-backup')
    const bar = screen.getByTestId('sign-footer')
    const scroller = document.createElement('div')
    Object.defineProperty(scroller, 'scrollHeight', { value: 900 })
    Object.defineProperty(scroller, 'clientHeight', { value: 700 })
    scroller.style.overflowY = 'auto'
    block.closest('[data-testid="click-panel"]')!.parentElement!.parentElement!.appendChild(scroller)
    scroller.appendChild(block.closest('[data-testid="click-panel"]')!)
    block.getBoundingClientRect = () => ({ bottom: 760 }) as DOMRect
    bar.getBoundingClientRect = () => ({ top: 690 }) as DOMRect
    fireEvent.focus(el)
    expect(scroller.scrollTop).toBe(78) // 760 - 690 + 8
  })
})

describe('the hub join review names the workspace', () => {
  const two = [{ id: WS, running: true }, { id: 'w_other', running: true }]
  it('with several workspaces the workspace line is on the main surface', async () => {
    mount(JOIN, two as never)
    await input()
    await waitFor(() => expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull(), T)
  })
  it('with one workspace it is folded under Details', async () => {
    mount(JOIN, [{ id: WS, running: true }] as never)
    await input()
    expect(screen.getByTestId('sign-workspace').closest('details')).not.toBeNull()
  })
})
