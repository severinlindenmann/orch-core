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
  expect(invalid()).toEqual(keys.slice(0, 4)) // Today's activity feed shows the event too
  seed(); invalidateLive(client, 'ws', 'items', [{ id: '1.n', type: 'needs.changed' }])
  expect(invalid()).toEqual([keys[3], keys[6]])
  for (const type of ['member.removed', 'role.changed', 'device.revoked']) {
    seed(); invalidateLive(client, 'ws', 'items', [{ id: '2.e', type }]); expect(invalid()).toEqual(keys.slice(0, 7))
  }
  for (const event of ['resync', 'ready']) {
    seed(); invalidateLive(client, 'ws', event, []); expect(invalid()).toEqual(keys.slice(0, 5))
  }
  seed(); invalidateLive(client, 'ws', 'items', [{ id: '3.e', type: 'settings.changed' }])
  expect(invalid()).toEqual([keys[4]]) // settings: only the settings queries and the identity card
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
it('visibility changes and gone purge the activity feed and the closed list, not only mark them stale', () => {
  const client = new QueryClient()
  const seed = () => {
    client.setQueryData(['today', 'ws'], { recent: [{ ticket: 'DEMO-0001', title: 'Secret title' }] })
    client.setQueryData(['tickets', 'ws', 'closed', {}], [{ uid: 'ULID0001', key: 'DEMO-0001', title: 'Secret title' }])
    client.setQueryData(['tickets', 'ws', 'all'], [{ uid: 'ULID0009', key: 'DEMO-0009', title: 'Other' }])
    client.setQueryData(['today', 'other'], { recent: [] })
  }
  for (const item of [{ id: '1.e', key: 'DEMO-0001', type: 'visibility.changed' }, { id: '1.e', key: 'DEMO-0001', type: 'ticket.reopened' }, { id: '1.g', uid: 'ULID0001', gone: true }]) {
    seed(); invalidateLive(client, 'ws', 'items', [item])
    expect(client.getQueryData(['today', 'ws'])).toBeUndefined()
    expect(client.getQueryData(['tickets', 'ws', 'closed', {}])).toBeUndefined()
    expect(client.getQueryData(['tickets', 'ws', 'all'])).toBeDefined()
    expect(client.getQueryData(['today', 'other'])).toEqual({ recent: [] })
  }
})
it('any successful frame, including a heartbeat, resets the failure counter', async () => {
  vi.useFakeTimers()
  const fetcher = vi.fn<typeof fetch>()
  fetcher.mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'))
  fetcher.mockResolvedValueOnce(new Response(new ReadableStream({ start(c) { c.enqueue(encode(': heartbeat\n\n')); c.close() } }),
    { headers: { 'content-type': 'text/event-stream' } }))
  fetcher.mockRejectedValue(new Error('offline'))
  const stop = startLiveUpdates('uuid', 'demo-not-a-real-token', vi.fn(), vi.fn(), fetcher)
  await vi.advanceTimersByTimeAsync(0)
  await vi.advanceTimersByTimeAsync(1000)
  await vi.advanceTimersByTimeAsync(2000)
  expect(fetcher).toHaveBeenCalledTimes(3)
  // The healthy heartbeat stream restarts the ladder at 1 s, not at 5 s.
  await vi.advanceTimersByTimeAsync(999)
  expect(fetcher).toHaveBeenCalledTimes(3)
  await vi.advanceTimersByTimeAsync(1)
  expect(fetcher).toHaveBeenCalledTimes(4)
  stop()
})
it('gone fails closed: removes every cached query that mentions the ticket, whatever its key or shape', () => {
  const client = new QueryClient()
  const uid = '01MNOPQRSTUVWXYZ0123456789', key = 'DEMO-0007'
  client.setQueryData(['ticket', 'ws', key], { uid, key })
  client.setQueryData(['search', 'ws', 'needle'], { hits: { groups: [{ tickets: [{ uid, key, title: 'secret' }] }] } })
  client.setQueryData(['needs', 'ws'], { needs: [{ ref: key, detail: 'x' }], unrelated: 1 })
  client.setQueryData(['x', { scope: 'ws' }, 'board'], { columns: [{ cards: [{ uid }] }] })
  client.setQueryData(['ticket', 'ws', 'DEMO-0001'], { uid: 'other-uid', key: 'DEMO-0001' })
  client.setQueryData(['ticket', 'other', key], { uid: 'foreign', key })
  invalidateLive(client, 'ws', 'items', [{ id: '1.g', uid, gone: true }])
  for (const k of [['ticket', 'ws', key], ['search', 'ws', 'needle'], ['needs', 'ws'], ['x', { scope: 'ws' }, 'board']])
    expect(client.getQueryData(k)).toBeUndefined()
  expect(client.getQueryData(['ticket', 'ws', 'DEMO-0001'])).toBeDefined()
  expect(client.getQueryData(['ticket', 'other', key])).toBeDefined()
})
it('a malformed gone purges the whole workspace instead of being skipped', () => {
  const client = new QueryClient()
  client.setQueryData(['tickets', 'ws'], { rows: [{ uid: 'a' }] })
  client.setQueryData(['today', 'other'], { keep: true })
  invalidateLive(client, 'ws', 'items', [{ id: '1.g', gone: true } as never])
  expect(client.getQueryData(['tickets', 'ws'])).toBeUndefined()
  expect(client.getQueryData(['today', 'other'])).toEqual({ keep: true })
})
it('revoked removes workspace data wherever the UUID sits, and cross-workspace caches; resync keeps the cache', () => {
  const client = new QueryClient()
  const seed = () => {
    client.setQueryData(['workspaces'], [{ id: 'ws', name: 'Secret workspace' }])
    client.setQueryData(['me'], { roster: ['ws'] })
    client.setQueryData(['search', 'q', 'ws'], { hits: 1 })
    client.setQueryData(['today', 'ws'], { needs: [] })
    client.setQueryData(['today', 'other'], { keep: true })
  }
  seed(); invalidateLive(client, 'ws', 'resync', [])
  expect(client.getQueryData(['today', 'ws'])).toBeDefined(); expect(client.getQueryData(['workspaces'])).toBeDefined()
  invalidateLive(client, 'ws', 'revoked', [])
  for (const k of [['workspaces'], ['me'], ['search', 'q', 'ws'], ['today', 'ws']]) expect(client.getQueryData(k)).toBeUndefined()
  expect(client.getQueryData(['today', 'other'])).toEqual({ keep: true })
})
it('a 403/404 on the stream is revocation, not an outage', async () => {
  for (const status of [403, 404]) {
    const receive = vi.fn(), fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('{}', { status }))
    const stop = startLiveUpdates('uuid', 'demo-not-a-real-token', receive, vi.fn(), fetcher)
    await new Promise((r) => setTimeout(r, 10))
    expect(receive).toHaveBeenCalledWith('revoked', []); expect(fetcher).toHaveBeenCalledTimes(1); stop()
  }
})

it('a child that becomes invisible disappears from the epic page cache; revoked purges the children and refusals caches', () => {
  const client = new QueryClient()
  client.setQueryData(['ticket-children', 'ws', 'DEMO-0040'], [{ uid: 'c1', key: 'DEMO-0043', title: 'Secret child' }, { uid: 'c2', key: 'DEMO-0044', title: 'Other' }])
  client.setQueryData(['tickets', 'ws'], { rows: [{ uid: 'c1', key: 'DEMO-0043' }] })
  invalidateLive(client, 'ws', 'items', [{ id: '1.g', uid: 'c1', gone: true }])
  expect(client.getQueryData(['ticket-children', 'ws', 'DEMO-0040'])).toBeUndefined()
  client.setQueryData(['ticket-children', 'ws', 'DEMO-0040'], [{ uid: 'c2', key: 'DEMO-0044' }])
  client.setQueryData(['ticket-refusals', 'ws', 'DEMO-0040', 3], [{ text: 'x' }])
  invalidateLive(client, 'ws', 'revoked', [])
  expect(client.getQueryData(['ticket-children', 'ws', 'DEMO-0040'])).toBeUndefined()
  expect(client.getQueryData(['ticket-refusals', 'ws', 'DEMO-0040', 3])).toBeUndefined()
})
it('gone passes the collected keys to close, so only the matching open ticket is closed', () => {
  const client = new QueryClient(), close = vi.fn()
  client.setQueryData(['tickets', 'ws'], { rows: [{ uid: 'u', key: 'DEMO-0001' }] })
  invalidateLive(client, 'ws', 'items', [{ id: '1.g', uid: 'u', gone: true }], close)
  expect(close).toHaveBeenCalledWith('u', expect.arrayContaining(['u', 'DEMO-0001']))
  expect(close.mock.calls[0][1]).not.toContain('DEMO-0002')
})
