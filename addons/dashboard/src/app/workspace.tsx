import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { toast } from 'sonner'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { workspaceOfTicket } from '@/api/workspaces'
import type { Workspace } from '@/api/types'

interface SwitchOptions {
  /** Open this ticket in the target workspace instead of keeping the current page. */
  ticket?: string
  /** Set by a switch guard when it lets the switch through. */
  guarded?: boolean
}

interface WorkspaceCtx {
  workspace: Workspace | undefined
  workspaces: Workspace[]
  /** Switches the current (mock) workspace; every workspace-keyed query refetches. Does not touch the page. */
  setWorkspaceId: (id: string) => void
  /** The user-facing switch: keeps the page where it still makes sense, otherwise leaves it with a toast. */
  switchWorkspace: (id: string, opts?: SwitchOptions) => void
}

const noop = () => {}
const Ctx = createContext<WorkspaceCtx>({ workspace: undefined, workspaces: [], setWorkspaceId: noop, switchWorkspace: noop })

const STORAGE_KEY = 'orch.workspace'

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

/**
 * A screen with unsaved work can ask before the workspace changes under it: while a guard is set, switchWorkspace
 * calls it with the switch as `proceed` and does nothing until the guard runs it.
 */
let switchGuard: ((proceed: () => void) => void) | null = null
export function useSwitchGuard(guard: ((proceed: () => void) => void) | null) {
  useEffect(() => {
    switchGuard = guard
    return () => {
      if (switchGuard === guard) switchGuard = null
    }
  }, [guard])
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const router = useRouter()
  const { data = [] } = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const [id, setId] = useState<string | null>(readStored)
  const setWorkspaceId = useCallback(
    (next: string) => {
      setId(next)
      try {
        localStorage.setItem(STORAGE_KEY, next)
      } catch {
        /* storage unavailable: the choice lasts for this page only */
      }
      void qc.invalidateQueries()
    },
    [qc],
  )
  const workspace = data.find((w) => w.id === id) ?? data[0]

  // Latest values for switchWorkspace, so its identity stays stable for key handlers.
  const latest = useRef({ data, workspace })
  useEffect(() => {
    latest.current = { data, workspace }
  })

  const switchWorkspace = useCallback(
    (next: string, opts: SwitchOptions = {}) => {
      const { data: all, workspace: current } = latest.current
      const target = all.find((w) => w.id === next)
      if (!target) return
      if (next === current?.id && !opts.ticket) return
      if (switchGuard && !opts.guarded) {
        switchGuard(() => switchWorkspace(next, { ...opts, guarded: true }))
        return
      }
      const path = router.state.location.pathname
      if (opts.ticket) {
        setWorkspaceId(next)
        void router.navigate({ to: '/ticket/$key', params: { key: opts.ticket } })
        return
      }
      if (next === current?.id) return
      const ticketKey = /^\/ticket\/([^/]+)$/.exec(path)?.[1]
      const addonName = /^\/addon\/([^/]+)\/[^/]+$/.exec(path)?.[1]
      setWorkspaceId(next)
      if (ticketKey) {
        // A ticket lives in one workspace: stay only when that is the one we are switching to.
        const home = workspaceOfTicket(ticketKey, all) ?? current
        if (home && home.id !== next) {
          void router.navigate({ to: '/tickets' })
          toast(`${ticketKey} is in ${home.name}`)
        }
      } else if (addonName && !addonActive(target, addonName)) {
        const title = qc.getQueryData<{ name: string; title: string }[]>(['addons'])?.find((a) => a.name === addonName)?.title ?? addonName
        void router.navigate({ to: '/' })
        toast(`${title} is not enabled in ${target.name}`)
      }
    },
    [qc, router, setWorkspaceId],
  )

  const value = useMemo<WorkspaceCtx>(() => ({ workspaces: data, workspace, setWorkspaceId, switchWorkspace }), [data, workspace, setWorkspaceId, switchWorkspace])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useWorkspace = () => useContext(Ctx)
