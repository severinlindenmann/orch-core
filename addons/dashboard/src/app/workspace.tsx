import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { toast } from 'sonner'
import { addonActive } from '@/api/addons'
import { addressHint, isRunning, preferWorkspace, uuid8, workspaceByAddress, workspaceOfTicket } from '@/api/workspaces'
import type { Workspace } from '@/api/types'
import { isWorkspacePath, prefixOfKey, setLinkWorkspace, splitWorkspacePath, ticketKeyOf, toPublicPath, type UrlState } from './urls'
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
  /** The workspaces of the viewer whose host does not answer: listed as "host not running", with no pages. */
  unavailable: Workspace[]
  /** The address names a prefix that two or more workspaces share, and no `?ws=` picks one: the page asks which. */
  ambiguous?: Workspace[]
  /** The address names (by `?ws=`) a workspace whose host is not running. */
  stopped?: Workspace
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
const Ctx = createContext<WorkspaceCtx>({ workspace: undefined, workspaces: [], unavailable: [], setWorkspaceId: noop, switchWorkspace: noop })

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
  const { data: everything = [], isSuccess } = useQuery(queries.workspacesAll())
  const data = useMemo(() => everything.filter(isRunning), [everything])
  const unavailable = useMemo(() => everything.filter((w) => !isRunning(w)), [everything])
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
  // `?ws=<last 8 hex of the UUID (WS8)>` tells two workspaces with one prefix apart (design 5).
  const urlHint = ownsUrl && urlPrefix !== undefined ? new URL(publicHref, 'http://x').searchParams.get('ws') : null
  // Prefixes match regardless of case (`/w/demo/board`); the address is then corrected to the real one.
  // A ticket address is the ticket key's workspace; it takes `?ws=` only where the key's prefix is shared (or a hint is given).
  const ticketKey = ticketKeyOf(inAppPath)
  const ticketPrefix = ticketKey === undefined ? undefined : prefixOfKey(ticketKey)
  const sharedKey = ticketPrefix !== undefined && data.filter((w) => w.prefix.toUpperCase() === ticketPrefix.toUpperCase()).length > 1
  const addressed = scoped ? urlPrefix : ownsUrl && urlPrefix !== undefined && (sharedKey || urlHint) ? ticketPrefix : undefined
  const named = addressed !== undefined ? workspaceByAddress(data, addressed, urlHint) : undefined
  const fromUrl = named && 'found' in named ? named.found : undefined
  // An address that names a workspace whose host cannot serve it (by `?ws=`, or by its prefix when no running workspace has
  // it) says so, with the host's reason; nothing else is shown instead.
  const stopped =
    !fromUrl && addressed !== undefined
      ? urlHint
        ? unavailable.find((w) => uuid8(w.id) === urlHint.toLowerCase())
        : named && 'missing' in named
          ? unavailable.find((w) => !!w.prefix && w.prefix.toUpperCase() === addressed.toUpperCase())
          : undefined
      : // #951: no address and no workspace answers (a fresh install whose dashboard is not granted yet): say why, not
        // "Could not load Today". The first one is shown; with several, the person fixes it and the rest follow.
        addressed === undefined && isSuccess && data.length === 0
        ? unavailable[0]
        : undefined
  const ambiguous = named && 'ambiguous' in named && !stopped ? named.ambiguous : undefined
  const missingPrefix = addressed !== undefined && isSuccess && named && 'missing' in named && !stopped ? addressed : undefined
  /** A `/w/<PREFIX>` address whose workspace is not known yet: the page waits instead of showing another workspace. */
  const pendingPrefix = ownsUrl && scoped && urlPrefix !== undefined && !isSuccess
  // While the address does not name one workspace (it asks which, or names a stopped host) none is current: nothing of
  // another workspace is read or drawn in its place.
  const workspace = ambiguous || stopped ? undefined : (fromUrl ?? data.find((w) => w.id === id) ?? data[0])
  // In-app links point into this workspace (the router's rewrite reads it when it builds an address).
  const linkPrefix = workspace?.prefix ?? null
  const linkHint = workspace ? addressHint(workspace, data) : null
  // A ticket key names a workspace by its prefix only: when two share it, the one the person is in wins.
  preferWorkspace(workspace?.id ?? null)
  // After every render: an incoming address may have pointed the rewrite at its own prefix (an unknown one too).
  useLayoutEffect(() => {
    if (router && ownsUrl) setLinkWorkspace(router, urls, linkPrefix, linkHint)
  })

  // An address for another workspace (pasted, Back/Forward) makes it the remembered one too.
  useEffect(() => {
    if (fromUrl && fromUrl.id !== id) remember(fromUrl.id)
    // Only when the address names another workspace, not when the remembered one changes under it.
  }, [fromUrl?.id])

  // Old and short addresses (`/board`, `/`) get the workspace; a ticket address gets (or is corrected to) the workspace
  // its key names (`/ticket/K`, `/w/OTHER/ticket/K` -> `/w/<K's workspace>/ticket/K`).
  const keyHome = ticketKey === undefined ? undefined : workspaceOfTicket(ticketKey, data)
  useEffect(() => {
    if (!workspace || missingPrefix || ambiguous || stopped) return
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
    // The hint goes with the address of the workspace the person is in (a key's own workspace: only if it is this one).
    const mine = target === workspace.prefix ? addressHint(workspace, data) : null
    const search = new URLSearchParams(l.searchStr)
    if (mine) search.set('ws', mine)
    const query = search.toString()
    router.history.replace(`${toPublicPath(l.pathname, target)}${query ? `?${query}` : ''}${l.hash ? `#${l.hash}` : ''}`, l.state, { ignoreBlocker: true })
  }, [workspace, fromUrl, missingPrefix, ambiguous, stopped, scoped, urlPrefix, router, ownsUrl, ticketKey, keyHome?.prefix, inAppPath])

  // Latest values for switchWorkspace, so its identity stays stable for key handlers.
  const latest = useRef({ data, workspace })
  useEffect(() => {
    latest.current = { data, workspace }
  })

  const setWorkspaceId = useCallback(
    (next: string, opts: { url?: 'push' | 'replace' | 'none' } = {}) => {
      const target = latest.current.data.find((w) => w.id === next)
      remember(next)
      const hint = target ? addressHint(target, latest.current.data) : null
      // Links and the navigation that may follow point into the new workspace right away.
      if (target && router && urls !== NO_URLS) setLinkWorkspace(router, urls, target.prefix, hint)
      const l = router?.latestLocation
      // A ticket page is left by the switch (a ticket lives in one workspace); only a choice at an ambiguous address stays on it.
      const here = isWorkspacePath(l?.pathname ?? '/') || (opts.url === 'replace' && ticketKeyOf(l?.pathname ?? '') !== undefined)
      if (router && l && urls !== NO_URLS && target && opts.url !== 'none' && here) {
        // The switch guards (useSwitchGuard) have already asked about unsaved work.
        const search = new URLSearchParams(l.searchStr)
        if (hint) search.set('ws', hint)
        const query = search.toString()
        const href = `${toPublicPath(l.pathname, target.prefix)}${query ? `?${query}` : ''}${l.hash ? `#${l.hash}` : ''}`
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
    () => ({ workspaces: data, unavailable, ambiguous, stopped, workspace, missingPrefix, pendingPrefix, setWorkspaceId, switchWorkspace }),
    [data, unavailable, ambiguous, stopped, workspace, missingPrefix, pendingPrefix, setWorkspaceId, switchWorkspace],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useWorkspace = () => useContext(Ctx)
