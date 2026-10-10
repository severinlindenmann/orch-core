// Permanent URLs (G2). The route tree keeps short in-app paths (`/board`, `/settings/$tab`, `/ticket/$key`); the
// address bar carries the workspace for every workspace page (`/w/DEMO/board`). A router rewrite maps between the
// two, so a pasted URL opens the same page in the same workspace, and every in-app link gets the current workspace.
//
//   in-app path              address bar
//   /                        /w/<PREFIX>            (Today of that workspace)
//   /board, /tickets, ...    /w/<PREFIX>/board, ...
//   /ticket/<KEY>            /ticket/<KEY>          (the key names its workspace)
//
// URLs carry keys and ids only: never titles, tokens or signed values.

import type { AnyRouter, LocationRewrite } from '@tanstack/react-router'

const WS_PATH = /^\/w\/([^/?#]+)(\/.*)?$/

/** The workspace prefix of an address-bar path and the in-app path after it (`/w/DEMO/board` -> DEMO, `/board`). */
export function splitWorkspacePath(pathname: string): { prefix?: string; path: string } {
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

/** Whether an in-app path belongs to one workspace (everything but a ticket, whose key carries its workspace). */
export function isWorkspacePath(path: string): boolean {
  return !path.startsWith('/ticket/') && !WS_PATH.test(path)
}

/** The address-bar path of an in-app path in workspace `prefix` (unchanged without a prefix or for tickets). */
export function toPublicPath(path: string, prefix: string | null | undefined): string {
  if (!prefix || !isWorkspacePath(path)) return path
  return `/w/${encodeURIComponent(prefix)}${path === '/' ? '' : path}`
}

/** Per-router URL state the rewrite reads: the workspace that in-app links point into. */
export interface UrlState {
  prefix: string | null
}

/** The router rewrite: strips `/w/<PREFIX>` on the way in, adds the current workspace on the way out. */
export function workspaceRewrite(state: UrlState): LocationRewrite {
  return {
    input: ({ url }) => {
      const { prefix, path } = splitWorkspacePath(url.pathname)
      if (prefix !== undefined) url.pathname = path
      return url
    },
    output: ({ url }) => {
      url.pathname = toPublicPath(url.pathname, state.prefix)
      return url
    },
  }
}

/**
 * Points in-app links at workspace `prefix`. The router caches the addresses it built for links, so it gets a new
 * rewrite (TanStack drops that cache and re-renders the links when the rewrite changes). Call from an effect or an
 * event handler, never during render.
 */
export function setLinkWorkspace(router: AnyRouter, urls: UrlState, prefix: string | null) {
  if (urls.prefix === prefix) return
  urls.prefix = prefix
  router.update({ ...router.options, rewrite: workspaceRewrite(urls) })
}

/** The link a person can paste elsewhere, for an address-bar href (`/w/DEMO/board?x=1`). */
export function shareableLink(publicHref: string, origin: string = window.location.origin): string {
  return `${origin}${publicHref}`
}
