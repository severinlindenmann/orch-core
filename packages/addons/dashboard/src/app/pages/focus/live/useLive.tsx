import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
import { liveQueries, type LiveAgent, type LiveCache, type LiveJob, type LiveSnapshot } from '@/api/live'
import { demoLiveOn } from '@/mocks/addons/focus/demo'

/** The live screens (agent tree, jobs, commands) are a preview on the mock host, for the demo ticket only. */
export const liveEnabled = (ticket?: string) => !isRealHost() && demoLiveOn(ticket)

export function useLiveSnapshot(ws: string, ticket?: string) {
  return useQuery({ ...liveQueries.snapshot(ws, ticket ?? ''), enabled: !!ws && liveEnabled(ticket) })
}

export interface LiveCtx {
  ws: string
  ticket: string
  snap: LiveSnapshot
  /** Agent or job id. */
  selected: string
  select: (id: string) => void
  details: boolean
  /** Whole seconds clock (ms) that ticks once a second, shared by every timer. */
  now: number
  openRequest: () => void
}

const Ctx = createContext<LiveCtx | null>(null)
export const LiveProvider = ({ value, children }: { value: LiveCtx; children: ReactNode }) => <Ctx.Provider value={value}>{children}</Ctx.Provider>
export const useLive = () => useContext(Ctx)

export function useClock(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  return now
}

export const fmtTokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : String(n))
export const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.max(0, Math.floor(s % 60))).padStart(2, '0')}`
export const secondsSince = (iso: string | null, now: number) => (iso ? Math.max(0, Math.floor((now - Date.parse(iso)) / 1000)) : null)

export interface CacheView { kind: 'warm' | 'cooling' | 'cold'; text: string; sub: string }
/** Warm / Going cold in m:ss / Cold, with what a cold start costs. */
export function cacheView(cache: LiveCache | null, now: number): CacheView | null {
  if (!cache || !cache.ttl) return null
  const left = cache.ttl - Math.floor((now - Date.parse(cache.lastActivity)) / 1000)
  if (left <= 0) return { kind: 'cold', text: 'Cold', sub: `next step re-reads ${fmtTokens(cache.tokens)} tokens · slower, ≈ $${cache.missUsd.toFixed(2)} more at API price` }
  if (left <= 90) return { kind: 'cooling', text: `Going cold in ${mmss(left)}`, sub: `${fmtTokens(cache.tokens)} tokens kept · answer before then to stay fast` }
  return { kind: 'warm', text: 'Warm', sub: `${fmtTokens(cache.tokens)} tokens kept · ${mmss(left)} left without activity` }
}
export const cacheBadgeText = (v: CacheView) => (v.kind === 'cold' ? 'cache cold' : v.kind === 'cooling' ? 'cache cooling' : 'cache warm')

/** How the agent is paid for: covered by a subscription, estimated API charges, or a local model. */
export function costKind(a: Pick<LiveAgent, 'tool' | 'account'>): 'local' | 'subscription' | 'api' {
  if (a.tool === 'local') return 'local'
  return /subscription|ChatGPT/.test(a.account) ? 'subscription' : 'api'
}

export const jobProgressPct = (j: LiveJob) => (j.progress.total ? Math.round((j.progress.done / j.progress.total) * 100) : 0)
export const agentById = (snap: LiveSnapshot, id: string) => snap.agents.find((a) => a.id === id)
export const jobById = (snap: LiveSnapshot, id: string) => snap.jobs.find((j) => j.id === id)
