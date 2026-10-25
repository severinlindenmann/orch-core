// #1198 C16: the GitHub token field. The server and the signer are faked at the api boundary; the seal is real.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import * as sealed from '@/api/sealed'
import { ApiError, type Workspace } from '@/api/types'
import { GithubToken } from './GithubToken'

const WS = 'a'.repeat(32)
const workspace = { id: WS, prefix: 'INT', name: 'orchestrator', click: null } as unknown as Workspace
const REQ = 'hr_01M5PVRB46WS3X8H5X2RQK04S9'
const DIGEST = 'sha256:' + 'a'.repeat(64)
const TOKEN = 'ghp_' + 'Ab3'.repeat(12)
const T = { timeout: 5000 }

let pk = ''
const offer = (purposes = ['github.token']) => ({ request_id: REQ, view_digest: DIGEST, expires_in: 300, seal: { v: 1 as const, alg: sealed.ALG, pk, purposes } })
let begin: ReturnType<typeof vi.spyOn>
let save: ReturnType<typeof vi.spyOn>

beforeEach(async () => {
  const pair = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])
  pk = sealed.b64urlEncode(new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey)))
  begin = vi.spyOn(api, 'githubSeal').mockImplementation(async () => offer())
  save = vi.spyOn(api, 'githubSave').mockResolvedValue({ credential: 'stored' })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

async function type(text: string) {
  fireEvent.click(screen.getByTestId('github-token-start'))
  const input = (await screen.findByTestId('github-token-input', {}, T)) as HTMLInputElement
  fireEvent.change(input, { target: { value: text } })
  return input
}

describe('GithubToken', () => {
  it('seals the token to the signer and sends only ciphertext', async () => {
    render(<GithubToken workspace={workspace} />)
    const input = await type(TOKEN)
    fireEvent.click(screen.getByTestId('github-token-save'))
    await screen.findByTestId('github-token-saved', {}, T)
    expect(input.value).toBe('')
    expect(save).toHaveBeenCalledTimes(1)
    const [ws, body] = save.mock.calls[0] as [string, { request_id: string; view_digest: string; sealed: Record<string, { v: number; ct: string }> }]
    expect(ws).toBe(WS)
    expect(body.request_id).toBe(REQ)
    expect(body.view_digest).toBe(DIGEST)
    expect(Object.keys(body.sealed)).toEqual(['github.token'])
    expect(JSON.stringify(body)).not.toContain(TOKEN)
    expect(document.body.textContent).not.toContain(TOKEN)
  })

  it('keeps the browser from offering to save the token as a password', async () => {
    const { container } = render(<GithubToken workspace={workspace} />)
    const input = await type(TOKEN)
    expect(container.querySelector('form')).toBeNull()
    expect(input.getAttribute('autocomplete')).toBe('new-password')
    expect(input.hasAttribute('data-1p-ignore')).toBe(true)
    expect(input.getAttribute('data-lpignore')).toBe('true')
  })

  it('refuses an offer for another purpose', async () => {
    begin.mockImplementation(async () => offer(['custody.passphrase']))
    render(<GithubToken workspace={workspace} />)
    fireEvent.click(screen.getByTestId('github-token-start'))
    await screen.findByTestId('github-token-error', {}, T)
    expect(screen.queryByTestId('github-token-input')).toBeNull()
  })

  it('shows the signer’s plain refusal, empties the field and asks for a fresh key', async () => {
    save.mockRejectedValueOnce(new ApiError(400, { code: 'invalid.input', message: 'that is not a GitHub token (ghp_..., github_pat_... or similar)', retryable: false } as never))
    render(<GithubToken workspace={workspace} />)
    const input = await type('nonsense')
    fireEvent.click(screen.getByTestId('github-token-save'))
    const err = await screen.findByTestId('github-token-error', {}, T)
    expect(err.textContent).toContain('not a GitHub token')
    await waitFor(() => expect(begin).toHaveBeenCalledTimes(2), T)
    expect(input.value).toBe('')
    expect(document.body.textContent).not.toContain('nonsense')
  })

  it('removes the token with one click', async () => {
    const remove = vi.spyOn(api, 'githubRemove').mockResolvedValue({ credential: 'missing' })
    render(<GithubToken workspace={workspace} />)
    fireEvent.click(screen.getByTestId('github-token-remove'))
    await screen.findByTestId('github-token-removed', {}, T)
    expect(remove).toHaveBeenCalledWith(WS)
  })
})
