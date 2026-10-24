// #1198 C7: the shown-secret grant. The server and the signer are faked at the api boundary; the sealed envelope is the
// shared vector (core sealed the plaintext to the key below), so the browser really opens it with WebCrypto.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ARM_MS, CLICK_RISK, keyFingerprint, REVEAL_LINE, REVEAL_RISK, type ClickViewAnswer } from '@/api/click'
import { b64urlEncode, RevealKey } from '@/api/sealed'
import { importKeyPair } from '@/api/sealed.testing'
import * as sealed from '@/api/sealed'
import { TooltipProvider } from '@/components/ui/tooltip'
import { GrantReveal } from './GrantReveal'

type Jwk = { x: string; y: string; d: string }
type V = { open: { name: string; expect: string; envelope: unknown; recipient_jwk: Jwk; bound: { workspace_id: string; request_id: string }; context: { view_digest: string } }[]; seal: { name: string; plaintext: string }[] }
const VEC = JSON.parse(readFileSync(resolve(process.cwd(), '../../plugins/orch-core/tests/click_sealed_vectors.json'), 'utf8')) as V
const OK = VEC.open.find((c) => c.name === 'out_grant_secret/ok')!
const SECRET = VEC.seal.find((c) => c.name === 'out_grant_secret')!.plaintext
const WS = OK.bound.workspace_id
const REQ = OK.bound.request_id
const DIGEST = OK.context.view_digest
const GRANT = 'gr_01M4Y46B485AXD4CBEJZ15Z1TV'
const T = { timeout: 5000 }

let pub = ''
const view = async (over: Partial<ClickViewAnswer['view']> = {}): Promise<ClickViewAnswer> => ({
  view_digest: DIGEST,
  view: {
    risk: CLICK_RISK, op: 'grant', workspace: { id: WS, name: 'billing', prefix: 'DEMO' }, fingerprint: '0123456789ab',
    lines: ['grant to agents, 48 hours', '', REVEAL_RISK, REVEAL_LINE], checks: [], confirm: { kind: 'click' },
    expires_at: new Date(Date.now() + 600_000).toISOString(), auth: 'convenience', device: 'd_x', artifacts: null,
    seal: { v: 1, alg: 'P256-HKDF-SHA256-A256GCM', purposes: ['grant.secret'], for: await keyFingerprint(pub) }, ...over,
  },
})

let s: Record<string, ReturnType<typeof vi.spyOn>>
let key: RevealKey
beforeEach(async () => {
  const pair = await importKeyPair(OK.recipient_jwk)
  key = new RevealKey(pair) // unbound: the component binds it when prepare answers
  pub = b64urlEncode(pair.publicKey)
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  vi.spyOn(sealed, 'newRevealKey').mockResolvedValue(key)
  s = {
    prepare: vi.spyOn(api, 'prepareClick').mockResolvedValue({ request_id: REQ, expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'grant', confirm: 'click' }),
    view: vi.spyOn(api, 'viewClick').mockResolvedValue(await view()),
    sign: vi.spyOn(api, 'signClick').mockResolvedValue({ state: 'submitted', duplicate: false, grant: GRANT, reveal: { purpose: 'grant.secret', envelope: OK.envelope as sealed.Envelope } }),
    cancel: vi.spyOn(api, 'cancelClick').mockResolvedValue({ id: REQ, state: 'cancelled' }),
  }
})
afterEach(async () => {
  cleanup()
  await new Promise((r) => setTimeout(r, 20))
  vi.restoreAllMocks()
})

function mount(onClose = vi.fn(), onBusy = vi.fn()) {
  const r = render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><TooltipProvider><GrantReveal ws={WS} hours={48} scope="workable" label="my tool" onClose={onClose} onBusy={onBusy} /></TooltipProvider></QueryClientProvider>)
  return { onClose, onBusy, unmount: r.unmount }
}
const signButton = async () => screen.findByRole('button', { name: 'Sign and show the secret' }, T)
async function signAndWait() {
  await signButton()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Sign and show the secret' })).toBeEnabled(), { timeout: ARM_MS + 3000 })
  const b = screen.getByRole('button', { name: 'Sign and show the secret' })
  fireEvent.pointerDown(b)
  fireEvent.click(b)
}

describe('the grant reveal', () => {
  it('sends its public key at prepare, with the grant words and nothing secret', async () => {
    mount()
    await signButton()
    expect(screen.getByTestId('grant-reveal-risk')).toHaveTextContent(REVEAL_RISK)
    expect(s.prepare).toHaveBeenCalledTimes(1)
    expect(s.prepare.mock.calls[0][1]).toEqual({ action: 'agent_access', hours: 48, scope: 'workable', label: 'my tool', reveal_key: pub })
    expect(JSON.stringify(s.prepare.mock.calls)).not.toContain(SECRET)
  })

  it('signs with the key the review was made for, opens the sealed secret once, shows it with a once-only note and Copy', async () => {
    const write = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: write }, configurable: true })
    mount()
    await signAndWait()
    const secret = await screen.findByTestId('grant-secret', {}, T)
    expect(secret).toHaveTextContent(SECRET)
    expect(s.sign).toHaveBeenCalledTimes(1)
    expect(s.sign.mock.calls[0].slice(1)).toEqual([REQ, DIGEST, pub])
    expect(screen.getByTestId('grant-reveal-once')).toHaveTextContent('shown only once')
    expect(key.closed).toBe(true) // one reveal per key
    fireEvent.click(screen.getByRole('button', { name: /Copy/ }))
    await waitFor(() => expect(write).toHaveBeenCalledWith(SECRET))
  })

  it('wipes the secret from the page when it is hidden or closed', async () => {
    const { onClose } = mount()
    await signAndWait()
    await screen.findByTestId('grant-secret', {}, T)
    fireEvent.click(screen.getByRole('button', { name: 'Done, hide it' }))
    expect(onClose).toHaveBeenCalled()
    expect(screen.queryByTestId('grant-secret')).toBeNull()
    expect(document.body.textContent).not.toContain(SECRET)
  })

  it('does not offer Sign for a review made for another key, another purpose, or without the risk line', async () => {
    for (const over of [{ seal: { v: 1, alg: 'x', purposes: ['grant.secret'], for: '0'.repeat(16) } }, { seal: undefined }, { lines: ['grant', REVEAL_LINE] }, { workspace: { id: 'f'.repeat(32), name: 'x', prefix: 'X' } }]) {
      s.view.mockResolvedValue(await view(over as Partial<ClickViewAnswer['view']>))
      mount()
      expect(await screen.findByTestId('grant-reveal-bad-view', {}, T)).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Sign and show the secret' })).toBeNull()
      cleanup()
      key = new RevealKey(await importKeyPair(OK.recipient_jwk))
      vi.mocked(sealed.newRevealKey).mockResolvedValue(key)
    }
    expect(s.sign).not.toHaveBeenCalled()
  })

  it('says so when the signature landed but no secret arrived', async () => {
    s.sign.mockResolvedValue({ state: 'submitted', duplicate: false, grant: GRANT, reveal: null })
    mount()
    await signAndWait()
    expect(await screen.findByTestId('grant-reveal-lost', {}, T)).toHaveTextContent('stop this access')
    expect(screen.queryByTestId('grant-secret')).toBeNull()
  })

  it('refuses an envelope that is for another request and shows no secret', async () => {
    s.prepare.mockResolvedValue({ request_id: 'hr_' + '0'.repeat(25) + '1', expires_at: new Date(Date.now() + 600_000).toISOString(), op: 'grant', confirm: 'click' })
    s.view.mockResolvedValue(await view())
    mount()
    await signAndWait()
    expect(await screen.findByTestId('grant-reveal-lost', {}, T)).toHaveTextContent('could not be opened in this window')
    expect(document.body.textContent).not.toContain(SECRET)
  })

  it('cancels a prepared, unsigned request when it goes away, and never signs', async () => {
    const { unmount } = mount()
    await signButton()
    unmount()
    await waitFor(() => expect(s.cancel).toHaveBeenCalledWith(WS, REQ), T)
    expect(s.sign).not.toHaveBeenCalled()
    expect(key.closed).toBe(true)
  })

  it('prepares nothing for a page that went away while its key was being made', async () => {
    let release: (k: RevealKey) => void = () => {}
    vi.mocked(sealed.newRevealKey).mockReturnValue(new Promise<RevealKey>((r) => { release = r }))
    const { unmount } = mount()
    unmount()
    release(key)
    await new Promise((r) => setTimeout(r, 50))
    expect(s.prepare).not.toHaveBeenCalled()
    expect(key.closed).toBe(true)
  })

  it('tells its dialog to stay open from the click on until the person is done', async () => {
    const { onBusy } = mount()
    await signAndWait()
    await screen.findByTestId('grant-secret', {}, T)
    expect(onBusy).toHaveBeenLastCalledWith(true)
    fireEvent.click(screen.getByRole('button', { name: 'Done, hide it' }))
  })
})
