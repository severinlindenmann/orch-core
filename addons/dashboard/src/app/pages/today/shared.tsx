import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import type { AgentInfo, Workspace } from '@/api/types'
import { toastApiError } from '@/app/toast'

/** Short, readable hash: first 8 and last 4 characters. */
export function shortHash(h: string) {
  return h.length > 14 ? `${h.slice(0, 8)}…${h.slice(-4)}` : h
}

/** Elapsed time between two ISO instants, as "14m", "2h 05m" or "3d". */
export function ago(from: string, now: string) {
  const mins = Math.max(0, Math.round((Date.parse(now) - Date.parse(from)) / 60_000))
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m`
  if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`
  return `${Math.floor(mins / 1440)}d`
}

export interface Directory {
  workspace?: Workspace
  agents: AgentInfo[]
}

/** person id (p_sev) or "agent:claude-code" -> display name. */
export function displayName(dir: Directory, id: string): string {
  if (id.startsWith('agent:')) {
    const a = dir.agents.find((x) => x.id === id.slice(6))
    return a ? `${a.name} for ${displayName(dir, a.for)}` : id.slice(6)
  }
  return dir.workspace?.members.find((m) => m.person === id)?.name ?? id
}

// ------------------------------------------------------------------ resolved items (optimistic hide + notes)

export interface Note {
  id: string
  text: string
  detail: string
}
interface ResolveCtx {
  act: (id: string, fn: () => Promise<unknown>, ok: { toast: string; note?: { text: string; detail: string } }) => Promise<boolean>
}
const Ctx = createContext<ResolveCtx | null>(null)

export function ResolveProvider({ children }: { children: (s: { hidden: Set<string>; notes: Note[] }) => ReactNode }) {
  const qc = useQueryClient()
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [notes, setNotes] = useState<Note[]>([])
  const act = useCallback<ResolveCtx['act']>(
    async (id, fn, ok) => {
      setHidden((h) => new Set(h).add(id))
      const unhide = () =>
        setHidden((h) => {
          const n = new Set(h)
          n.delete(id)
          return n
        })
      try {
        await fn()
        toast.success(ok.toast)
        if (ok.note) setNotes((n) => [{ id, ...ok.note! }, ...n].slice(0, 4))
        await qc.invalidateQueries()
        unhide()
        return true
      } catch (e) {
        unhide()
        toastApiError(e, 'That did not work.')
        return false
      }
    },
    [qc],
  )
  const value = useMemo(() => ({ act }), [act])
  return <Ctx.Provider value={value}>{children({ hidden, notes })}</Ctx.Provider>
}

export function useAct() {
  const v = useContext(Ctx)
  if (!v) throw new Error('ResolveProvider missing')
  return v.act
}
