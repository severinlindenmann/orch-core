import { useEffect, useState } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { clearSession, getToken } from './bootstrap'
import { readSSE } from './sse'

export type LiveItem = { id: string; uid?: string; key?: string | null; type?: string; gone?: boolean }
type CloseView = (uid?: string) => void

export function invalidateLive(client: QueryClient, ws: string, event: string, items: LiveItem[], close: CloseView = () => {}) {
  const scoped = { predicate: (q: { queryKey: readonly unknown[] }) => q.queryKey[1] === ws }
  const invalidate = (name: string) => { void client.invalidateQueries({ queryKey: [name, ws] }) }
  if (event === 'revoked') {
    void client.cancelQueries(scoped); client.removeQueries(scoped); close()
    void client.invalidateQueries({ queryKey: ['workspaces'] })
    void client.invalidateQueries({ queryKey: ['me'] })
    return
  }
  if (event === 'resync' || event === 'ready') { void client.invalidateQueries(scoped); return }
  for (const item of items) {
    if (item.gone) {
      // uid-only gone: discover keys from cached detail and list projections before removing.
      const keys = new Set<string>([item.uid!])
      for (const q of client.getQueryCache().findAll(scoped)) {
        const value = q.state.data as { uid?: string; key?: string; rows?: { uid: string; key: string }[] } | undefined
        if (value && value.uid === item.uid && value.key) keys.add(value.key)
        for (const row of value?.rows ?? []) if (row.uid === item.uid) keys.add(row.key)
      }
      const ticket = { predicate: (q: { queryKey: readonly unknown[]; state: { data: unknown } }) =>
        q.queryKey[1] === ws && (q.queryKey[0] === 'ticket' && keys.has(String(q.queryKey[2])) ||
          (q.state.data as { uid?: string } | undefined)?.uid === item.uid) }
      void client.cancelQueries(ticket); client.removeQueries(ticket); close(item.uid)
      // Remove stale restricted rows immediately, before a refetch can finish.
      client.setQueriesData(scoped, (value: unknown) => {
        if (!value || typeof value !== 'object') return value
        const data = value as Record<string, unknown>
        const next = { ...data }
        for (const field of ['rows', 'needs_you']) if (Array.isArray(data[field]))
          next[field] = (data[field] as { uid?: string }[]).filter((row) => row.uid !== item.uid)
        return next
      })
      for (const name of ['tickets', 'board', 'today']) invalidate(name)
    } else if (item.type === 'needs.changed') {
      invalidate('today'); void client.invalidateQueries({ queryKey: ['workspaces'] })
    } else if (item.type?.startsWith('member.') || item.type === 'role.changed' || item.type?.startsWith('device.')) {
      void client.invalidateQueries(scoped); void client.invalidateQueries({ queryKey: ['me'] })
      void client.invalidateQueries({ queryKey: ['workspaces'] })
    } else if (item.key) {
      void client.invalidateQueries({ queryKey: ['ticket', ws, item.key] }); invalidate('tickets'); invalidate('board')
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
      if (!response.ok || !response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('Streaming unavailable')
      await readSSE(response.body, (message) => {
        if (controller.signal.aborted) return
        const value: unknown = JSON.parse(message.data)
        if (message.id) last = message.id
        if (message.event === 'ready') { polling = false; pollEpoch += 1; clearTimeout(pollTimer); ready() }
        if (message.event === 'items') failures = 0
        if (message.event === 'revoked') { receive('revoked', []); controller.abort(); return }
        receive(message.event, Array.isArray(value) ? value as LiveItem[] : [])
      })
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
