import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { toast } from 'sonner'
import { addonActive } from '@/api/addons'
import { workspaceOfTicket } from '@/api/workspaces'
import type { Workspace } from '@/api/types'
import { isWorkspacePath, setLinkWorkspace, splitWorkspacePath, ticketKeyOf, toPublicPath, type UrlState } from './urls'
import { queries } from '@/api/queries'

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
  /** A `/w/<PREFIX>` address while the workspaces are still loading (the page shows a skeleton). */
  pendingPrefix?: boolean
  /**
   * Switches the current (mock) workspace; every workspace-keyed query refetches. Keeps the page; on a workspace page
   * the address moves to the new workspace (a new history entry, so Back returns to the old one).
   */
  setWorkspaceId: (id: string, opts?: { url?: 'push' | 'replace' | 'none' }) => void
  /** The user-facing switch: keeps the page where it still makes sense, otherwise leaves it with a toast. */
  switchWorkspace: (id: string, opts?: SwitchOptions) => void
}

const noop = () => {}
const Ctx = createContext<WorkspaceCtx>({ workspace: undefined, workspaces: [], setWorkspaceId: noop, switchWorkspace: noop })

/** The address-bar path of an href (with its `/w/<PREFIX>`). */
const publicPathOf = (publicHref: string) => new URL(publicHref, 'http://x').pathname
/** Outside the app's router (a component test with its own router, or none): links are left as they are. */
const NO_URLS: UrlState = { prefix: null }
const noSubscribe = () => () => {}

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
  const urls = (router?.options.context as { urls?: UrlState } | undefined)?.urls ?? NO_URLS
  const { data = [], isSuccess } = useQuery(queries.workspaces())
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
  // The address bar itself (the history), read without the router's state so tests without a router still work.
  const history = router?.history
  const publicHref = useSyncExternalStore(history ? (cb) => history.subscribe(cb) : noSubscribe, () => history?.location.href ?? '/')
  const ownsUrl = urls !== NO_URLS
  const split = ownsUrl ? splitWorkspacePath(publicPathOf(publicHref)) : { prefix: undefined, path: publicPathOf(publicHref) }
  const { prefix: urlPrefix, path: inAppPath } = split
  const scoped = isWorkspacePath(inAppPath)
  // Prefixes match regardless of case (`/w/demo/board`); the address is then corrected to the real one.
  const fromUrl = scoped && urlPrefix !== undefined ? data.find((w) => w.prefix.toUpperCase() === urlPrefix.toUpperCase()) : undefined
  const missingPrefix = scoped && urlPrefix !== undefined && isSuccess && !fromUrl ? urlPrefix : undefined
  /** A `/w/<PREFIX>` address whose workspace is not known yet: the page waits instead of showing another workspace. */
  const pendingPrefix = ownsUrl && scoped && urlPrefix !== undefined && !isSuccess
  const workspace = fromUrl ?? data.find((w) => w.id === id) ?? data[0]
  // In-app links point into this workspace (the router's rewrite reads it when it builds an address).
  const linkPrefix = workspace?.prefix ?? null
  // After every render: an incoming address may have pointed the rewrite at its own prefix (an unknown one too).
  useLayoutEffect(() => {
    if (router && ownsUrl) setLinkWorkspace(router, urls, linkPrefix)
  })

  // An address for another workspace (pasted, Back/Forward) makes it the remembered one too.
  useEffect(() => {
    if (fromUrl && fromUrl.id !== id) remember(fromUrl.id)
    // Only when the address names another workspace, not when the remembered one changes under it.
  }, [fromUrl?.id])

  // Old and short addresses (`/board`, `/`) get the workspace; a ticket address gets (or is corrected to) the workspace
  // its key names (`/ticket/K`, `/w/OTHER/ticket/K` -> `/w/<K's workspace>/ticket/K`).
  const ticketKey = ticketKeyOf(inAppPath)
  const keyHome = ticketKey === undefined ? undefined : workspaceOfTicket(ticketKey, data)
  useEffect(() => {
    if (!workspace || missingPrefix) return
    let target: string | undefined
    if (ticketKey !== undefined) {
      if (keyHome && (keyHome.prefix !== urlPrefix || inAppPath.endsWith('/'))) target = keyHome.prefix
    } else {
      const miscased = !!fromUrl && fromUrl.prefix !== urlPrefix
      if (miscased || scoped !== (urlPrefix !== undefined)) target = workspace.prefix
    }
    if (!target || !router || !ownsUrl) return
    const l = router.latestLocation
    // The same page under its full address: not a navigation a page with unsaved work needs to ask about.
    router.history.replace(`${toPublicPath(l.pathname, target)}${l.searchStr}${l.hash ? `#${l.hash}` : ''}`, l.state, { ignoreBlocker: true })
  }, [workspace, fromUrl, missingPrefix, scoped, urlPrefix, router, ownsUrl, ticketKey, keyHome?.prefix, inAppPath])

  // Latest values for switchWorkspace, so its identity stays stable for key handlers.
  const latest = useRef({ data, workspace })
  useEffect(() => {
    latest.current = { data, workspace }
  })

  const setWorkspaceId = useCallback(
    (next: string, opts: { url?: 'push' | 'replace' | 'none' } = {}) => {
      const target = latest.current.data.find((w) => w.id === next)
      remember(next)
      // Links and the navigation that may follow point into the new workspace right away.
      if (target && router && urls !== NO_URLS) setLinkWorkspace(router, urls, target.prefix)
      const l = router?.latestLocation
      if (router && l && urls !== NO_URLS && target && opts.url !== 'none' && isWorkspacePath(l.pathname)) {
        // The switch guards (useSwitchGuard) have already asked about unsaved work.
        const href = `${toPublicPath(l.pathname, target.prefix)}${l.searchStr}${l.hash ? `#${l.hash}` : ''}`
        if (opts.url === 'replace') router.history.replace(href, l.state, { ignoreBlocker: true })
        else router.history.push(href, undefined, { ignoreBlocker: true })
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
    () => ({ workspaces: data, workspace, missingPrefix, pendingPrefix, setWorkspaceId, switchWorkspace }),
    [data, workspace, missingPrefix, pendingPrefix, setWorkspaceId, switchWorkspace],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useWorkspace = () => useContext(Ctx)
