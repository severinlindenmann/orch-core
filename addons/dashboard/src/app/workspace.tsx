import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter, useRouterState } from '@tanstack/react-router'
import { toast } from 'sonner'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { workspaceOfTicket } from '@/api/workspaces'
import type { Workspace } from '@/api/types'
import { isWorkspacePath, splitWorkspacePath, toPublicPath } from './urls'

interface SwitchOptions {
  /** Open this ticket in the target workspace instead of keeping the current page. */
  ticket?: string
  /** Set by a switch guard when it lets the switch through. */
  guarded?: boolean
}

interface WorkspaceCtx {
  workspace: Workspace | undefined
  workspaces: Workspace[]
  /** The prefix of a `/w/<PREFIX>/…` address that names no workspace of the viewer's (the page shows "not found"). */
  missingPrefix?: string
  /**
   * Switches the current (mock) workspace; every workspace-keyed query refetches. Keeps the page; on a workspace page
   * the address moves to the new workspace (a new history entry, so Back returns to the old one).
   */
  setWorkspaceId: (id: string, opts?: { url?: 'push' | 'none' }) => void
  /** The user-facing switch: keeps the page where it still makes sense, otherwise leaves it with a toast. */
  switchWorkspace: (id: string, opts?: SwitchOptions) => void
}

const noop = () => {}
const Ctx = createContext<WorkspaceCtx>({ workspace: undefined, workspaces: [], setWorkspaceId: noop, switchWorkspace: noop })

/** The address-bar href of the current location (with its `/w/<PREFIX>`). */
const publicPathOf = (publicHref: string) => new URL(publicHref, 'http://x').pathname

const STORAGE_KEY = 'orch.workspace'

function readStored(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

/**
 * A screen with unsaved work can ask before the workspace changes under it. While guards are set, switchWorkspace
 * runs them one after the other, each with "go on" as `proceed`, and switches only when every guard has let it
 * through (a guard that says no simply never calls `proceed`). More than one screen can hold a guard at once.
 */
type SwitchGuard = (proceed: () => void) => void
const switchGuards: SwitchGuard[] = []
export function useSwitchGuard(guard: SwitchGuard | null) {
  useEffect(() => {
    if (!guard) return
    switchGuards.push(guard)
    return () => {
      const at = switchGuards.indexOf(guard)
      if (at >= 0) switchGuards.splice(at, 1)
    }
  }, [guard])
}

/** Asks each guard in turn; `done` runs when all agreed. */
function askGuards(list: SwitchGuard[], done: () => void) {
  const [first, ...rest] = list
  if (!first) return done()
  first(() => askGuards(rest, done))
}

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const router = useRouter()
  const urls = router.options.context.urls
  const { data = [], isSuccess } = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const [id, setId] = useState<string | null>(readStored)
  const remember = useCallback((next: string) => {
    setId(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }, [])

  // The address bar decides on workspace pages (`/w/DEMO/board`); elsewhere (a ticket) the last workspace stays.
  const loc = useRouterState({ select: (s) => ({ pathname: s.location.pathname, publicHref: s.location.publicHref }), structuralSharing: true })
  const scoped = isWorkspacePath(loc.pathname)
  const urlPrefix = splitWorkspacePath(publicPathOf(loc.publicHref)).prefix
  const fromUrl = scoped && urlPrefix !== undefined ? data.find((w) => w.prefix === urlPrefix) : undefined
  const missingPrefix = scoped && urlPrefix !== undefined && isSuccess && !fromUrl ? urlPrefix : undefined
  const workspace = fromUrl ?? data.find((w) => w.id === id) ?? data[0]
  // In-app links point into this workspace (the router's rewrite reads it when it builds an address).
  urls.prefix = workspace?.prefix ?? null

  // An address for another workspace (pasted, Back/Forward) makes it the remembered one too.
  useEffect(() => {
    if (fromUrl && fromUrl.id !== id) remember(fromUrl.id)
    // Only when the address names another workspace, not when the remembered one changes under it.
  }, [fromUrl?.id])

  // Old and short addresses (`/board`, `/`) get the workspace; a ticket address loses one (its key names it).
  useEffect(() => {
    if (!workspace || missingPrefix) return
    if (scoped === (urlPrefix !== undefined)) return
    const l = router.state.location
    router.history.replace(`${toPublicPath(l.pathname, workspace.prefix)}${l.searchStr}${l.hash ? `#${l.hash}` : ''}`, l.state)
  }, [workspace, missingPrefix, scoped, urlPrefix, router])

  // Latest values for switchWorkspace, so its identity stays stable for key handlers.
  const latest = useRef({ data, workspace })
  useEffect(() => {
    latest.current = { data, workspace }
  })

  const setWorkspaceId = useCallback(
    (next: string, opts: { url?: 'push' | 'none' } = {}) => {
      const target = latest.current.data.find((w) => w.id === next)
      remember(next)
      if (target) urls.prefix = target.prefix
      const l = router.state.location
      if (target && opts.url !== 'none' && isWorkspacePath(l.pathname)) {
        router.history.push(`${toPublicPath(l.pathname, target.prefix)}${l.searchStr}${l.hash ? `#${l.hash}` : ''}`)
      }
      void qc.invalidateQueries()
    },
    [qc, remember, router, urls],
  )

  const switchWorkspace = useCallback(
    (next: string, opts: SwitchOptions = {}) => {
      const { data: all, workspace: current } = latest.current
      const target = all.find((w) => w.id === next)
      if (!target) return
      if (next === current?.id && !opts.ticket) return
      if (switchGuards.length > 0 && !opts.guarded) {
        askGuards([...switchGuards], () => switchWorkspace(next, { ...opts, guarded: true }))
        return
      }
      const path = router.state.location.pathname
      if (opts.ticket) {
        const changed = next !== current?.id
        setWorkspaceId(next, { url: 'none' })
        void router.navigate({ to: '/ticket/$key', params: { key: opts.ticket } })
        if (changed) toast(`Switched to ${target.name} to open ${opts.ticket}`)
        return
      }
      if (next === current?.id) return
      const ticketKey = /^\/ticket\/([^/]+)$/.exec(path)?.[1]
      const addonName = /^\/addon\/([^/]+)\/[^/]+$/.exec(path)?.[1]
      const leaves = addonName && !addonActive(target, addonName)
      setWorkspaceId(next, { url: leaves ? 'none' : 'push' })
      if (ticketKey) {
        // A ticket lives in one workspace: stay only when that is the one we are switching to.
        const home = workspaceOfTicket(ticketKey, all) ?? current
        if (home && home.id !== next) {
          void router.navigate({ to: '/tickets' })
          toast(`${ticketKey} is in ${home.name}`)
        }
      } else if (leaves) {
        const title = qc.getQueryData<{ name: string; title: string }[]>(['addons'])?.find((a) => a.name === addonName)?.title ?? addonName
        void router.navigate({ to: '/' })
        toast(`${title} is not enabled in ${target.name}`)
      }
    },
    [qc, router, setWorkspaceId],
  )

  const value = useMemo<WorkspaceCtx>(
    () => ({ workspaces: data, workspace, missingPrefix, setWorkspaceId, switchWorkspace }),
    [data, workspace, missingPrefix, setWorkspaceId, switchWorkspace],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useWorkspace = () => useContext(Ctx)
