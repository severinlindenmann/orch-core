import { afterEach, expect, it, vi } from 'vitest'
import { createFetchTransport } from './fetchTransport'

afterEach(() => vi.unstubAllGlobals())
it('sends bearer headers, no cookies, and preserves error envelopes', async () => {
  const error = { exit: 3, error: { code: 'role.denied', message: 'Not allowed', hint: 'Ask owner', retryable: false } }
  const fetcher = vi.fn(async () => Response.json(error, { status: 403 }))
  vi.stubGlobal('fetch', fetcher)
  const transport = createFetchTransport('', () => 'demo-not-a-real-token')
  expect(await transport.request('GET', '/api/session')).toEqual({ status: 403, json: error })
  expect(fetcher).toHaveBeenCalledWith('/api/session', expect.objectContaining({
    headers: { Authorization: 'Bearer demo-not-a-real-token' }, credentials: 'omit', redirect: 'error', cache: 'no-store',
  }))
  await transport.request('POST', '/auth/logout', {})
  expect(fetcher).toHaveBeenLastCalledWith('/auth/logout', expect.objectContaining({ headers: {
    Authorization: 'Bearer demo-not-a-real-token', 'content-type': 'application/json',
  }, body: '{}' }))
})
it('C3: announces click contract 4 on the click routes only', async () => {
  const fetcher = vi.fn(async () => Response.json({}))
  vi.stubGlobal('fetch', fetcher)
  const transport = createFetchTransport('', () => 'demo-not-a-real-token')
  await transport.request('POST', `/api/w/${'a'.repeat(32)}/click/prepare`, { action: 'close', key: 'DEMO-1' })
  expect(fetcher).toHaveBeenLastCalledWith(`/api/w/${'a'.repeat(32)}/click/prepare`, expect.objectContaining({ headers: {
    Authorization: 'Bearer demo-not-a-real-token', 'content-type': 'application/json', 'X-Orch-Click-Contract': '4',
  } }))
  await transport.request('GET', `/api/w/${'a'.repeat(32)}/settings`)
  expect(fetcher).toHaveBeenLastCalledWith(`/api/w/${'a'.repeat(32)}/settings`, expect.objectContaining({ headers: { Authorization: 'Bearer demo-not-a-real-token' } }))
})
it('rejects cross-origin bases and paths before credentials leave', async () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  await expect(createFetchTransport('https://evil.test').request('GET', '/api/session')).rejects.toThrow('same-origin')
  const transport = createFetchTransport('')
  for (const path of ['//evil.test', '/\\evil.test', 'https://evil.test']) await expect(transport.request('GET', path)).rejects.toThrow()
  expect(fetcher).not.toHaveBeenCalled()
})
it('handles empty or non-JSON responses without crashing', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('proxy failed', { status: 502 })))
  expect((await createFetchTransport('').request('GET', '/api/session')).json).toMatchObject({ error: { code: 'bad_response' } })
})
