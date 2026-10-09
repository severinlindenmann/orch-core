// The guide's route-to-page map is data the guide addon sends; this picks the page for a path. Pure: core and tests share it.

/** `match` is an exact path, or a prefix when it ends with `*` ("/ticket/*" matches /ticket/DEMO-0043). */
export interface HelpRoute {
  match: string
  page: string
}

export const DEFAULT_HELP_PAGE = 'getting-around'

/** The slug of the first route that matches `path`, else the default page. */
export function helpPageFor(routes: HelpRoute[], path: string): string {
  const clean = path.split(/[?#]/)[0] || '/'
  const hit = routes.find((r) => (r.match.endsWith('*') ? clean.startsWith(r.match.slice(0, -1)) : clean === r.match))
  return hit?.page ?? DEFAULT_HELP_PAGE
}
