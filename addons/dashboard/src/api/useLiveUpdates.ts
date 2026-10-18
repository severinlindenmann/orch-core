import { useEffect, useState } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { clearSession, getToken } from './bootstrap'
import { readSSE } from './sse'
import { mergeRunItems, type RunItem } from './runs'

export type LiveItem = { id: string; uid?: string; key?: string | null; type?: string; gone?: boolean }
type CloseView = (uid?: string, keys?: string[]) => void

/** Fail closed: anything we cannot prove unrelated is treated as related. */
const mentions = (value: unknown, needles: string[]) => {
  try { const text = JSON.stringify(value) ?? ''; return needles.some((n) => text.includes(n)) } catch { return true }
}
type Q = { queryKey: readonly unknown[]; state: { data: unknown } }

export function invalidateLive(client: QueryClient, ws: string, event: string, items: LiveItem[], close: CloseView = () => {}) {
  // Any key that mentions the workspace UUID anywhere, not only at the expected position.
  const scoped = { predicate: (q: Q) => q.queryKey[1] === ws || mentions(q.queryKey, [ws]) }
  const invalidate = (name: string) => { void client.invalidateQueries({ queryKey: [name, ws] }) }
  const purgeWorkspace = () => {
    void client.cancelQueries(scoped); client.removeQueries(scoped); close()
    // Cross-workspace caches can carry this workspace's names, counts and roster: drop them, not just mark stale.
    for (const name of ['workspaces', 'me']) { void client.cancelQueries({ queryKey: [name] }); client.removeQueries({ queryKey: [name] }) }
    void client.invalidateQueries({ queryKey: ['workspaces'] })
    void client.invalidateQueries({ queryKey: ['me'] })
  }
  if (event === 'revoked') { purgeWorkspace(); return }
  // Runs (M7a): merged by revision; a reconnect or the stream's end comes as resync, which reads the full list again.
  if (event === 'runs') { mergeRunItems(client, ws, items as unknown as RunItem[]); return }
  if (event === 'resync' || event === 'ready') { void client.invalidateQueries(scoped); return }
  for (const item of items) {
    if (item.gone) {
      if (typeof item.uid !== 'string' || !item.uid) { purgeWorkspace(); continue } // malformed target: purge broadly
      const uid = item.uid
      // uid-only gone: discover keys from every cached projection before removing.
      const keys = new Set<string>([uid])
      const collect = (value: unknown, depth = 0) => {
        if (!value || typeof value !== 'object' || depth > 6) return
        if (Array.isArray(value)) { for (const v of value) collect(v, depth + 1); return }
        const row = value as Record<string, unknown>
        if (row.uid === uid && typeof row.key === 'string') keys.add(row.key)
        for (const v of Object.values(row)) collect(v, depth + 1)
      }
      for (const q of client.getQueryCache().findAll(scoped)) collect(q.state.data)
      const needles = [...keys]
      // Remove detail and sub-resource queries by key or content; any query that still mentions the ticket goes too.
      const ticket = { predicate: (q: Q) => (q.queryKey[1] === ws || mentions(q.queryKey, [ws])) &&
        (q.queryKey.some((part) => typeof part === 'string' && keys.has(part)) || mentions(q.state.data, needles)) }
      // Strip the known list shapes first (keeps lists mounted without flicker), then remove whatever still mentions it.
      client.setQueriesData(scoped, (value: unknown) => {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return value
        const data = value as Record<string, unknown>
        const next = { ...data }
        for (const field of ['rows', 'needs_you', 'items']) if (Array.isArray(data[field]))
          next[field] = (data[field] as { uid?: string }[]).filter((row) => row?.uid !== uid)
        return next
      })
      void client.cancelQueries(ticket); client.removeQueries(ticket); close(uid, needles)
      // The feed on Today and the closed list name the ticket by key and title: purge them whole, then read as the new viewer.
      const feeds = { predicate: (q: Q) => q.queryKey[1] === ws && (q.queryKey[0] === 'today' || q.queryKey[0] === 'activity' || (q.queryKey[0] === 'tickets' && q.queryKey.includes('closed'))) }
      void client.cancelQueries(feeds); client.removeQueries(feeds)
      for (const name of ['tickets', 'board', 'today']) invalidate(name)
    } else if (item.type === 'needs.changed') {
      invalidate('today'); void client.invalidateQueries({ queryKey: ['workspaces'] })
    } else if (item.type?.startsWith('member.') || item.type === 'role.changed' || item.type?.startsWith('device.')) {
      void client.invalidateQueries(scoped); void client.invalidateQueries({ queryKey: ['me'] })
      void client.invalidateQueries({ queryKey: ['workspaces'] })
    } else if (item.type === 'settings.changed' || item.type === 'policy.changed' || item.type?.startsWith('grant.')) {
      // Settings, gate policies and grants (M4). A non-owner is not sent others' grant items, so the grants list is also
      // read again on focus and on a timer; an item that does arrive makes it current at once.
      for (const name of ['settings', 'members', 'settings-grants', 'identity']) invalidate(name)
    } else if (item.type === 'visibility.changed' || item.type === 'people.changed' || item.type === 'ticket.closed' || item.type === 'ticket.reopened') {
      // Who may see a ticket (or whether it is listed) changed. Today's activity and a closed list carry titles and rows
      // that were visible a moment ago: drop them, not only mark them stale, then read again as the new viewer.
      const lists = { predicate: (q: Q) => q.queryKey[1] === ws && (q.queryKey[0] === 'today' || q.queryKey[0] === 'activity' || (q.queryKey[0] === 'tickets' && q.queryKey.includes('closed'))) }
      void client.cancelQueries(lists); client.removeQueries(lists)
      if (item.key) void client.invalidateQueries({ queryKey: ['ticket', ws, item.key] })
      for (const name of ['tickets', 'board', 'today']) invalidate(name)
    } else if (item.key) {
      void client.invalidateQueries({ queryKey: ['ticket', ws, item.key] }); invalidate('tickets'); invalidate('board'); invalidate('today')
    } else { void client.invalidateQueries(scoped) }
  }
}

const backoff = [1000, 2000, 5000, 10000]
export function startLiveUpdates(ws: string, token: string, receive: (event: string, items: LiveItem[]) => void,
  ready: () => void, fetcher: typeof fetch = fetch) {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined, pollTimer: ReturnType<typeof setTimeout> | undefined
  let last = '', failures = 0, head: string | undefined, polling = false, pollEpoch = 0
  const later = (fn: () => void, ms: number) => { if (!controller.signal.aborted) timer = setTimeout(fn, ms) }
  const headers = () => ({ Authorization: `Bearer ${token}`, ...(last ? { 'Last-Event-ID': last } : {}) })
  const options = () => ({ headers: headers(), signal: controller.signal, credentials: 'omit' as const, redirect: 'error' as const, cache: 'no-store' as const })
  async function poll(epoch: number) {
    if (controller.signal.aborted || !polling || epoch !== pollEpoch) return
    try {
      const response = await fetcher(`/api/w/${encodeURIComponent(ws)}/head`, options())
      if (response.status === 401) { clearSession(true, token); return }
      if (!response.ok) throw new Error('Polling unavailable')
      const value = await response.json() as { sub_cursor: string }
      if (controller.signal.aborted || !polling || epoch !== pollEpoch) return
      if (head !== value.sub_cursor) { head = value.sub_cursor; receive('resync', []) }
      ready()
    } catch { /* A later poll retries; no stale response changes the workspace selection. */ }
    if (!controller.signal.aborted && polling && epoch === pollEpoch) pollTimer = setTimeout(() => { void poll(epoch) }, 10000)
  }
  async function connect() {
    try {
      const response = await fetcher(`/api/events?ws=${encodeURIComponent(ws)}`, options())
      if (response.status === 401) { clearSession(true, token); return }
      // The host no longer pins or allows this workspace for us: revoked, never an outage. Fail closed.
      if (response.status === 403 || response.status === 404) { receive('revoked', []); controller.abort(); return }
      if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('Streaming unavailable')
      await readSSE(response.body, (message) => {
        if (controller.signal.aborted) return
        const value: unknown = JSON.parse(message.data)
        if (message.id) last = message.id
        if (message.event === 'ready') { polling = false; pollEpoch += 1; clearTimeout(pollTimer); ready() }
        if (message.event === 'revoked') { receive('revoked', []); controller.abort(); return }
        receive(message.event, Array.isArray(value) ? value as LiveItem[] : [])
      }, () => { failures = 0 }) // any received bytes, including a heartbeat comment, prove a healthy connection
      throw new Error('Stream ended')
    } catch {
      if (controller.signal.aborted) return
      failures += 1
      if (failures >= 3 && !polling) { polling = true; ready(); receive('resync', []); void poll(++pollEpoch) }
      later(() => { void connect() }, backoff[Math.min(failures - 1, 3)])
    }
  }
  void connect()
  return () => { controller.abort(); clearTimeout(timer); clearTimeout(pollTimer) }
}

/** Only the production shell mounts this hook; mock transport keeps its existing behavior. */
export function useLiveUpdates(ws: string | undefined, close?: CloseView) {
  const client = useQueryClient()
  const [readyWorkspace, setReadyWorkspace] = useState<string>()
  const [revokedWorkspace, setRevokedWorkspace] = useState<string>()
  useEffect(() => {
    const token = getToken()
    if (!ws || !token) return
    setReadyWorkspace(undefined)
    setRevokedWorkspace(undefined)
    return startLiveUpdates(ws, token, (event, items) => {
      if (event === 'revoked') { setReadyWorkspace(undefined); setRevokedWorkspace(ws) }
      invalidateLive(client, ws, event, items, close)
    }, () => setReadyWorkspace(ws))
  }, [client, ws, close])
  return { ready: !!ws && ws === readyWorkspace, revoked: !!ws && ws === revokedWorkspace }
}
