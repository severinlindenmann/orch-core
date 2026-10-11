import { afterEach, expect, it, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { invalidateLive, startLiveUpdates } from './useLiveUpdates'
import { readSSE } from './sse'
const encode = (s: string) => new TextEncoder().encode(s)
afterEach(() => { vi.useRealTimers() })
it('parses split UTF-8, CRLF, comments and multiline data', async () => {
  const messages: unknown[] = []
  const bytes = encode(': heartbeat\r\nid: n\r\nevent: items\r\ndata: é\r\ndata: two\r\n\r\n')
  await readSSE(new ReadableStream({ start(c) { for (const b of bytes) c.enqueue(new Uint8Array([b])); c.close() } }), (m) => messages.push(m))
  expect(messages).toEqual([{ event: 'items', data: 'é\ntwo', id: 'n' }])
})
it('reconnects with bearer and per-tab id, backs off and falls back to polling', async () => {
  vi.useFakeTimers()
  const fetcher = vi.fn<typeof fetch>()
  fetcher.mockResolvedValueOnce(new Response(new ReadableStream({ start(c) {
    c.enqueue(encode('event: ready\nid: instance:1\ndata: {}\n\n')); c.close()
  } }), { headers: { 'content-type': 'text/event-stream' } }))
  fetcher.mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'))
  fetcher.mockResolvedValue(new Response(JSON.stringify({ sub_cursor: 'head' })))
  const ready = vi.fn(), receive = vi.fn()
  const stop = startLiveUpdates('uuid', 'demo-not-a-real-token', receive, ready, fetcher)
  await vi.advanceTimersByTimeAsync(0)
  expect(ready).toHaveBeenCalledOnce()
  await vi.advanceTimersByTimeAsync(999)
  expect(fetcher).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(1)
  expect(fetcher.mock.calls[1][1]?.headers).toEqual({ Authorization: 'Bearer demo-not-a-real-token', 'Last-Event-ID': 'instance:1' })
  await vi.advanceTimersByTimeAsync(1999)
  expect(fetcher).toHaveBeenCalledTimes(2)
  await vi.advanceTimersByTimeAsync(1)
  expect(fetcher.mock.calls[3][0]).toBe('/api/w/uuid/head')
  expect(receive).toHaveBeenCalledWith('resync', [])
  stop(); await vi.advanceTimersByTimeAsync(30000)
  expect(fetcher).toHaveBeenCalledTimes(4)
})
it('implements the invalidation table scoped by UUID', () => {
  const client = new QueryClient()
  const keys = [['ticket', 'ws', 'DEMO-0001'], ['tickets', 'ws'], ['board', 'ws'], ['today', 'ws'], ['identity', 'ws'], ['me'], ['workspaces'], ['today', 'other']]
  const seed = () => keys.forEach((key) => client.setQueryData(key, {}))
  const invalid = () => keys.filter((key) => client.getQueryState(key)?.isInvalidated)
  seed(); invalidateLive(client, 'ws', 'items', [{ id: '1.e', key: 'DEMO-0001' }])
  expect(invalid()).toEqual(keys.slice(0, 3))
  seed(); invalidateLive(client, 'ws', 'items', [{ id: '1.n', type: 'needs.changed' }])
  expect(invalid()).toEqual([keys[3], keys[6]])
  for (const type of ['member.removed', 'role.changed', 'device.revoked']) {
    seed(); invalidateLive(client, 'ws', 'items', [{ id: '2.e', type }]); expect(invalid()).toEqual(keys.slice(0, 7))
  }
  for (const event of ['resync', 'ready']) {
    seed(); invalidateLive(client, 'ws', event, []); expect(invalid()).toEqual(keys.slice(0, 5))
  }
  seed(); invalidateLive(client, 'ws', 'items', [{ id: '3.e', type: 'settings.changed' }])
  expect(invalid()).toEqual(keys.slice(0, 5))
})
it('gone cancels and purges details and rows, closes views; revoked purges workspace', () => {
  const client = new QueryClient(), close = vi.fn(), cancel = vi.spyOn(client, 'cancelQueries')
  client.setQueryData(['ticket', 'ws', 'DEMO-0001'], { uid: 'u', key: 'DEMO-0001' })
  client.setQueryData(['ticket', 'ws', 'DEMO-0001', 'log'], { log: [] })
  client.setQueryData(['tickets', 'ws'], { rows: [{ uid: 'u', key: 'DEMO-0001' }] })
  client.setQueryData(['ticket', 'other', 'DEMO-0001'], { uid: 'other-u' })
  invalidateLive(client, 'ws', 'items', [{ id: '1.g', uid: 'u', gone: true }], close)
  expect(client.getQueryData(['ticket', 'ws', 'DEMO-0001'])).toBeUndefined()
  expect(client.getQueryData(['ticket', 'ws', 'DEMO-0001', 'log'])).toBeUndefined()
  expect(client.getQueryData(['tickets', 'ws'])).toEqual({ rows: [] })
  expect(cancel).toHaveBeenCalled(); expect(close).toHaveBeenCalledWith('u', expect.arrayContaining(['u', 'DEMO-0001']))
  invalidateLive(client, 'ws', 'revoked', [], close)
  expect(client.getQueryData(['tickets', 'ws'])).toBeUndefined()
  expect(client.getQueryData(['ticket', 'other', 'DEMO-0001'])).toEqual({ uid: 'other-u' })
  expect(close).toHaveBeenCalledWith()
})
