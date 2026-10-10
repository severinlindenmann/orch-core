// Search params of the pages that keep view state in the URL (G2). Every field is optional and parsed on its own:
// an invalid value is dropped (the page default applies), never a crash. Only keys and ids, never titles or secrets.
import { z } from 'zod'
import type { TabId } from './pages/ticket/shared'

const TICKET_TABS = ['overview', 'acceptance', 'changes', 'questions', 'artifacts', 'history', 'raw'] as const satisfies readonly TabId[]
const BOARD_VIEWS = ['board', 'list'] as const
const ARTIFACT_VIEWS = ['list', 'grid'] as const

/** A short plain value (an id, a key, a search text) of at most 200 characters (the router reads `q=42` as a number). */
const text = z.union([z.string(), z.number()]).transform(String).pipe(z.string().min(1).max(200))
const flag = z.union([z.literal(true), z.literal(1), z.literal('1'), z.literal('true')]).transform(() => true as const)

/** Parses each field with its schema; a field that fails is left out. */
function tolerant<S extends Record<string, z.ZodType>>(shape: S) {
  return (raw: Record<string, unknown>): { [K in keyof S]?: z.output<S[K]> } => {
    const out: Record<string, unknown> = {}
    for (const [k, schema] of Object.entries(shape)) {
      if (raw[k] === undefined) continue
      const r = schema.safeParse(raw[k])
      if (r.success) out[k] = r.data
    }
    return out as { [K in keyof S]?: z.output<S[K]> }
  }
}

/** /ticket/$key?tab=history (Overview is the default and is left out). */
export const validateTicketSearch = tolerant({ tab: z.enum(TICKET_TABS) })
export type TicketSearch = ReturnType<typeof validateTicketSearch>

/** /board?view=list&mine=1&type=bug&label=x&person=p_sev&epic=DEMO-0001&q=text */
export const validateBoardSearch = tolerant({
  view: z.enum(BOARD_VIEWS),
  mine: flag,
  type: text,
  label: text,
  person: text,
  epic: text,
  q: text,
})
export type BoardSearch = ReturnType<typeof validateBoardSearch>

/** /artifacts?view=grid&a=<artifact id> */
export const validateArtifactsSearch = tolerant({ view: z.enum(ARTIFACT_VIEWS), a: text })
export type ArtifactsSearch = ReturnType<typeof validateArtifactsSearch>
