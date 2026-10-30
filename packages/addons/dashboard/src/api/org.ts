// Company skills and tools offered by the hub (M3 H-5). The server relays the host's org.* ops in a closed shape. All
// text here came from the company hub: it is shown as plain text, never as markdown or HTML.
import { queryOptions } from '@tanstack/react-query'
import { api } from '@/api/client'

export type OrgKind = 'skill' | 'mcp'
export type OrgStatus = 'off' | 'on' | 'changed' | 'needs_isolation'

export interface OrgRow { name: string; digest: string; status: OrgStatus }
export interface OrgServerRow extends OrgRow { auth: 'none' | 'hub_secret' }

/** `GET /api/w/<ws>/org`: `available: false` is a host without the company ops. */
export interface OrgOffers { available: boolean; hub_id: string | null; skills: OrgRow[]; mcp: OrgServerRow[] }

/** One tool as the host pins it. `too_long`: too long to review, so no pin and no On switch. `input_schema` is pretty-printed JSON text. */
export interface OrgTool { name: string; title?: string | null; description: string | null; input_schema?: string | null; pin: string; status: 'on' | 'off' | 'changed' | 'too_long' }

/** Server-controlled text drawn as plain text: C0/C1 control characters (U+0000-U+001F except newline, U+007F-U+009F) and format, line and paragraph separators (bidi, zero-width) are removed first. */
export const plain = (s: string | null | undefined): string => (s ?? '').replace(/[\u0000-\u0009\u000b-\u001f\u007f-\u009f]|\p{Cf}|\p{Zl}|\p{Zp}/gu, '')

export interface OrgSkillItem { kind: 'skill'; name: string | null; digest: string | null; text: string | null; text_hidden: boolean }
export interface OrgServerItem {
  kind: 'mcp'; name: string | null; digest: string | null; url: string | null; auth: 'none' | 'hub_secret'
  secret: string | null; description: string | null; tools: OrgTool[]
  /** A field of the server cannot be shown (hidden characters): it cannot be turned on. */
  fields_hidden?: boolean
  /** Tools the host listed that could not be shown at all (they stay off). */
  hidden_tools?: number
}

/** `GET /api/w/<ws>/org/item`: what the host shows the owner before a click. `item.digest` is what Turn on sends back. */
export interface OrgShown {
  kind: OrgKind
  status: OrgStatus
  item: OrgSkillItem | OrgServerItem
  notes: string[]
  untrusted: boolean
  needs_isolation: string | null
}

/** One company secret this workspace was given. Never the value, never a length or a hash of it. `expires_ms` is null when unknown. */
export type OrgSecretStatus = 'ready' | 'expired' | 'not_received' | 'needs_isolation'
export interface OrgSecret { name: string; status: OrgSecretStatus; expires_ms: number | null; used_by: string[] }
/** `GET /api/w/<ws>/org/secrets` (#1434): `available: false` is a host without the op. */
export interface OrgSecrets { available: boolean; secrets: OrgSecret[] }

export const SECRET_STATUS_WORD: Record<OrgSecretStatus, string> = {
  ready: 'Ready',
  expired: 'Expired',
  not_received: 'Not shared any more',
  needs_isolation: 'Needs a separate agent user',
}

export const orgQueries = {
  secrets: (ws: string) => queryOptions({ queryKey: ['org', ws, 'secrets'], queryFn: () => api.getOrgSecrets(ws), retry: false }),
  offers: (ws: string) => queryOptions({ queryKey: ['org', ws, 'offers'], queryFn: () => api.getOrgOffers(ws), retry: false }),
  item: (ws: string, kind: OrgKind, name: string) =>
    queryOptions({ queryKey: ['org', ws, 'item', kind, name], queryFn: () => api.getOrgItem(ws, kind, name), retry: false, gcTime: 0 }),
}

/** The word on a row. */
export const STATUS_WORD: Record<OrgStatus, string> = {
  off: 'Off',
  on: 'On',
  changed: 'Changed — look again',
  needs_isolation: 'Needs a separate agent user',
}

/** `org.accept` answers `org: changed` when the item is no longer the version that was shown. */
export const isChanged = (e: unknown): boolean => /org: changed/.test(e instanceof Error ? e.message : '')
