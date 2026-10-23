// The text of an evidence file (receipt, log, notes), read through the dashboard server's typed route (reads.py). The
// server reads the file in chunks, checks its sha256 against the ticket's record and says whether it is text. Nothing
// here renders: the page shows the text escaped (components/sign/visible), never as HTML.
import { queryOptions } from '@tanstack/react-query'
import { call } from './runs'
import type { Artifact } from './types'

export interface ArtifactContent {
  name: string
  size: number
  digest: string
  /** The whole file was read and matched its recorded digest. */
  verified: boolean
  /** The server read only the first part of a very large file. */
  truncated: boolean
  binary: boolean
  text: string | null
  /** A picture, when the bytes are one (#1069). The page re-hashes it (`verifiedMedia`) before it is shown. */
  media?: EvidenceMedia | null
  /** A person's pasted screenshot (png or webp): the same kind of offer, kept under its older name. */
  image?: EvidenceMedia | null
}

/** How much of a text the page shows. */
export const TEXT_PREVIEW_BYTES = 64 * 1024

/** Files shown as plain text. HTML is not here: it keeps its own sandboxed viewer and its own switch. */
const TEXT_NAME = /\.(log|txt|json|jsonl|md|markdown|csv|diff|patch)$/i
const TEXT_KINDS: Artifact['kind'][] = ['log', 'receipt', 'feedback']

/** An artifact with a file of its own whose name or kind says text (never an add-on's artifact, a link or a picture). */
export function isTextArtifact(a: Pick<Artifact, 'name' | 'kind' | 'bytes' | 'addon' | 'url'>): boolean {
  if (a.addon || a.url || a.bytes == null) return false
  if (/\.html?$/i.test(a.name)) return false
  return TEXT_NAME.test(a.name) || TEXT_KINDS.includes(a.kind)
}

/** The first `TEXT_PREVIEW_BYTES` bytes of `text` as text (never ending in half a character), and whether it was cut. */
export function capText(text: string, limit = TEXT_PREVIEW_BYTES): { shown: string; cut: boolean; bytes: number } {
  const bytes = new TextEncoder().encode(text)
  if (bytes.length <= limit) return { shown: text, cut: false, bytes: bytes.length }
  let shown = new TextDecoder('utf-8').decode(bytes.subarray(0, limit))
  if (shown.endsWith('�')) shown = shown.slice(0, -1)
  return { shown, cut: true, bytes: bytes.length }
}

/** A file with a NUL byte near the start is binary, whatever its name says (the same rule as the explorer). */
export const looksBinary = (text: string): boolean => text.slice(0, 8192).includes('\0')

export const artifactTextQuery = (ws: string, ticket: string, name: string, sha256: string) =>
  queryOptions({
    queryKey: ['artifact-text', ws, ticket, name, sha256],
    queryFn: () => call<ArtifactContent>('GET', `/api/w/${encodeURIComponent(ws)}/tickets/${encodeURIComponent(ticket)}/artifacts/${encodeURIComponent(name)}`),
    retry: false,
    staleTime: 60_000,
  })

/** The digest the host read equals the one the ticket lists (either may carry the `sha256:` prefix). */
export const sameDigest = (read: string, listed: string): boolean => {
  const bare = (d: string) => d.replace(/^sha256:/i, '').toLowerCase()
  return bare(read) !== '' && bare(read) === bare(listed)
}

/** A picture the server offers for an evidence file (complete, digest-verified read). Shown only as an <img> (never markup). */
export interface EvidenceMedia {
  type: 'png' | 'jpeg' | 'gif' | 'webp' | 'svg'
  b64: string
  digest: string
}
