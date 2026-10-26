import { useMemo } from 'react'
import { useQueries } from '@tanstack/react-query'
import { api } from '@/api/client'
import { verifiedUrl } from '@/api/pasteImage'

const PASTE = /^paste-\d{8}-\d{6}-[0-9a-f]{8}\.(png|webp)$/
/** Only a picture a PERSON signed in as a pasted screenshot is drawn; an agent's image stays a placeholder with a link. */
export const isPersonPaste = (a: { name: string; kind?: string; added_by_kind?: string | null }): boolean => a.kind === 'screenshot' && PASTE.test(a.name) && a.added_by_kind === 'person'

const REF = /\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)/g

/**
 * R3 part B: the pictures a ticket's text shows. The server hands over the bytes of a file artifact; they are hashed
 * here and shown only when the hash is the one the signed log names (`artifacts[].sha256`). A picture that fails stays
 * a text placeholder. Nothing else is ever loaded: markdown images with any other address are never fetched.
 */
export function useTicketPictures(
  ws: string | undefined,
  ticketKey: string,
  artifacts: readonly { name: string; kind?: string; added_by_kind?: string | null; sha256: string; bytes: number | null; url?: string; addon?: string }[],
  texts: readonly string[],
): Record<string, string> {
  const wanted = useMemo(() => {
    const names = new Set<string>()
    for (const t of texts) for (const m of t.matchAll(REF)) names.add(m[1])
    return artifacts.filter((a) => names.has(a.name) && !a.url && !a.addon && a.sha256 && (a.bytes ?? 0) > 0 && (a.bytes ?? 0) <= 5 * 1024 * 1024 && isPersonPaste(a))
  }, [artifacts, texts])
  const results = useQueries({
    queries: wanted.map((a) => ({
      queryKey: ['ticket-picture', ws, ticketKey, a.name, a.sha256],
      enabled: !!ws,
      staleTime: Infinity,
      retry: false,
      queryFn: async () => {
        const got = await api.getArtifactImage(ws as string, ticketKey, a.name)
        return got.image ? (verifiedUrl(got.image, a.sha256) ?? null) : null
      },
    })),
  })
  return useMemo(() => {
    const out: Record<string, string> = {}
    wanted.forEach((a, i) => {
      const url = results[i]?.data
      if (url) out[a.name] = url
    })
    return out
  }, [wanted, results])
}
