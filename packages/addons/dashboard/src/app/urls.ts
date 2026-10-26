// Permanent URLs (G2). The route tree keeps short in-app paths (`/board`, `/settings/$tab`, `/ticket/$key`); the
// address bar carries the workspace for every workspace page (`/w/DEMO/board`). A router rewrite maps between the
// two, so a pasted URL opens the same page in the same workspace, and every in-app link gets the current workspace.
//
//   in-app path              address bar
//   /                        /w/<PREFIX>            (Today of that workspace)
//   /board, /tickets, ...    /w/<PREFIX>/board, ...
//   /ticket/<KEY>            /w/<KEY's PREFIX>/ticket/<KEY>   (the key names its workspace, not the current one;
//                            /ticket/<KEY> and /w/<OTHER>/ticket/<KEY> redirect to it)
//
// URLs carry keys and ids only: never titles, tokens or signed values.

import type { AnyRouter, LocationRewrite } from '@tanstack/react-router'

const WS_PATH = /^\/w\/([^/?#]+)(\/.*)?$/

/** The workspace prefix of an address-bar path and the in-app path after it (`/w/DEMO/board` -> DEMO, `/board`). */
export function splitWorkspacePath(pathname: string): { prefix?: string; path: string } {
  // `/w` or `/w/` alone names no workspace: Today of the current one.
  if (pathname === '/w' || pathname === '/w/') return { path: '/' }
  const m = WS_PATH.exec(pathname)
  if (!m) return { path: pathname }
  let prefix: string
  try {
    prefix = decodeURIComponent(m[1])
  } catch {
    prefix = m[1]
  }
  return { prefix, path: m[2] && m[2] !== '/' ? m[2] : '/' }
}

/** The key of an in-app ticket path (`/ticket/DEMO-0043` -> DEMO-0043). */
export function ticketKeyOf(path: string): string | undefined {
  const m = /^\/ticket\/([^/]+)\/?$/.exec(path)
  if (!m) return undefined
  try {
    return decodeURIComponent(m[1])
  } catch {
    return m[1]
  }
}

/** The workspace prefix a ticket key names (`DEMO-0043` -> DEMO). */
export const prefixOfKey = (key: string): string | undefined => /^(.+)-\d+$/.exec(key)?.[1]

/** Whether an in-app path takes the current workspace (everything but a ticket, whose key names its workspace). */
export function isWorkspacePath(path: string): boolean {
  return !path.startsWith('/ticket/') && path !== '/checklist' && path !== '/feedback' && !WS_PATH.test(path)
}

/** The address-bar path of an in-app path in workspace `prefix` (a ticket's workspace is its key's, whatever `prefix` is). */
export function toPublicPath(path: string, prefix: string | null | undefined): string {
  const key = ticketKeyOf(path)
  const own = key === undefined ? undefined : prefixOfKey(key)
  if (own) return `/w/${encodeURIComponent(own)}/ticket/${encodeURIComponent(key!)}`
  if (!prefix || !isWorkspacePath(path)) return path
  return `/w/${encodeURIComponent(prefix)}${path === '/' ? '' : path}`
}

/**
 * Per-router URL state the rewrite reads: the workspace that in-app links point into. When two workspaces share a prefix
 * the links also carry `?ws=<last 8 hex of the UUID (WS8)>` (design 5): `hint` is that value for `prefix`.
 */
export interface UrlState {
  prefix: string | null
  /** The `?ws=` of the workspace the links point into; null when its prefix is not shared. */
  hint?: string | null
  /** The `?ws=` of a ticket address (`/w/DEMO/ticket/DEMO-0001?ws=…`): the ticket's workspace when its key's prefix is shared. */
  ticketHint?: { prefix: string; hint: string } | null
  /** Set when an incoming address changed `prefix` behind the router's back: its cached link addresses are stale. */
  stale?: boolean
  /** The last address the rewrite read. */
  lastInput?: string
}

/** The router rewrite: strips `/w/<PREFIX>` on the way in, adds the current workspace on the way out. */
export function workspaceRewrite(state: UrlState): LocationRewrite {
  return {
    input: ({ url }) => {
      const { prefix, path } = splitWorkspacePath(url.pathname)
      // The incoming address wins: a redirect the router builds while loading it (`/w/INT/settings` ->
      // settings/general) stays in that workspace. WorkspaceProvider corrects it right after (unknown prefix).
      // Only for a new address: the router parses the same one again on reloads and invalidations.
      const fresh = url.href !== state.lastInput
      state.lastInput = url.href
      // `?ws=` belongs to the address, not to a page: the pages never see it.
      const hint = prefix === undefined ? null : url.searchParams.get('ws')
      if (hint !== null) url.searchParams.delete('ws')
      if (fresh) state.ticketHint = hint !== null && prefix !== undefined && ticketKeyOf(path) !== undefined ? { prefix, hint } : null
      // A ticket's key names its workspace: its address never moves the links' workspace.
      if (fresh && prefix !== undefined && ticketKeyOf(path) === undefined && (prefix !== state.prefix || (hint ?? null) !== (state.hint ?? null))) {
        state.prefix = prefix
        state.hint = hint
        state.stale = true
      }
      url.pathname = path
      return url
    },
    output: ({ url }) => {
      url.pathname = toPublicPath(url.pathname, state.prefix)
      // Links into the workspace the person is in carry its hint, so a shared prefix still names one workspace.
      const own = splitWorkspacePath(url.pathname).prefix?.toUpperCase()
      const hint = own === undefined ? null : state.hint && state.prefix?.toUpperCase() === own ? state.hint : state.ticketHint?.prefix.toUpperCase() === own ? state.ticketHint.hint : null
      if (hint) url.searchParams.set('ws', hint)
      return url
    },
  }
}

/**
 * Points in-app links at workspace `prefix`. The router caches the addresses it built for links, so it gets a new
 * rewrite (TanStack drops that cache and re-renders the links when the rewrite changes). Call from an effect or an
 * event handler, never during render.
 */
export function setLinkWorkspace(router: AnyRouter, urls: UrlState, prefix: string | null, hint: string | null = null) {
  if (urls.prefix === prefix && (urls.hint ?? null) === hint && !urls.stale) return
  urls.prefix = prefix
  urls.hint = hint
  urls.stale = false
  router.update({ ...router.options, rewrite: workspaceRewrite(urls) })
}

/** The link a person can paste elsewhere, for an address-bar href (`/w/DEMO/board?x=1`). */
export function shareableLink(publicHref: string, origin: string = window.location.origin): string {
  return `${origin}${publicHref}`
}
