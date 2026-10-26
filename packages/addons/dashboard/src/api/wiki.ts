// The Wiki of a real host (add-on): pages are markdown files in the project's wiki folder, read at HEAD by the dashboard
// server. One write: "Create the wiki folder" when the project has none (run-9 feedback 3.3).
import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export interface WikiRow {
  slug: string
  title: string
  updated: string | null
  by: string | null
  tickets: string[]
  links: number
  too_large: boolean
}
export interface WikiList {
  available: boolean
  /** `ok`, or why there is nothing to list: `no_repo`, `no_commits`, `no_folder`. */
  state: 'ok' | 'no_repo' | 'no_commits' | 'no_folder'
  message: string | null
  pages: WikiRow[]
  total: number
  cut: boolean
}
export interface WikiPage extends WikiRow {
  markdown: string | null
  size: number
}

export const wikiQueries = {
  list: (ws: string) => queryOptions({ queryKey: ['wiki', ws], queryFn: () => call<WikiList>('GET', `${w(ws)}/wiki`), retry: false }),
  page: (ws: string, slug: string) =>
    queryOptions({ queryKey: ['wiki', ws, 'page', slug], queryFn: () => call<WikiPage>('GET', `${w(ws)}/wiki/page?slug=${encodeURIComponent(slug)}`), retry: false }),
}

/** Makes `wiki/readme.md` in the project and commits just that file. Owners and maintainers; the server decides. */
export const createWikiFolder = (ws: string) => call<{ created: boolean; path: string }>('POST', `${w(ws)}/wiki/create`, {})
