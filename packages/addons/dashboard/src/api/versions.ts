// Settings > General > Versions: what this install runs (GET /api/w/:ws/versions). Read only; the add-ons' versions
// come with the Add-ons list (`AddOnRow.version`). Every field may be missing: an older host says no release.
import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'

export interface Versions {
  core: { version: string | null; release: string | null }
  dashboard: { version: string | null }
}

const SHAPE = /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$/
const RELEASE = /^[0-9a-f]{12}$/
const text = (v: unknown, re: RegExp): string | null => (typeof v === 'string' && re.test(v) ? v : null)

/** The checked answer: anything that is not a plain version or a 12-hex release reads as "not known". */
export function versionsOf(raw: unknown): Versions {
  const r = (raw && typeof raw === 'object' ? raw : {}) as { core?: Record<string, unknown>; dashboard?: Record<string, unknown> }
  return {
    core: { version: text(r.core?.version, SHAPE), release: text(r.core?.release, RELEASE) },
    dashboard: { version: text(r.dashboard?.version, SHAPE) },
  }
}

export const versionQueries = {
  get: (ws: string) =>
    queryOptions({ queryKey: ['versions', ws], queryFn: async () => versionsOf(await call('GET', `/api/w/${encodeURIComponent(ws)}/versions`)), retry: false, staleTime: 60_000 }),
}

/** "v1.2.3" for a version that does not already start with a v. */
export const vLabel = (v: string): string => (/^v/i.test(v) ? v : `v${v}`)
