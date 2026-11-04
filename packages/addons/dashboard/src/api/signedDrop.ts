// A signature the host has confirmed settles its item at once: Today and the approvals leave it out without waiting for
// the next read of "what needs you" (which can still list it for a moment). The server's list stays the truth: an entry
// ends when the server stops listing the item, and after DROP_MS at the latest, so a reopened item comes back.
import type { QueryClient } from '@tanstack/react-query'
import type { NeedsYouItem, QuestionStatus, TicketDocument, TodayDocument } from './types'

const DROP_MS = 30_000
interface Drop { ticket: string; ref: string | undefined; hash?: string | null; at: number }
const bare = (h: string) => h.replace(/^sha256:/, '')
const drops = new Map<string, Drop[]>()

// A step re-requested at a new version carries another hash: it is a new item, not the one that was signed.
const sameVersion = (a?: string | null, b?: string | null) => !a || !b || bare(a).startsWith(bare(b)) || bare(b).startsWith(bare(a))
const same = (i: NeedsYouItem, d: Drop) => i.ticket === d.ticket && (d.ref === undefined || i.ref === d.ref) && sameVersion(i.hash, d.hash)

/** Today without the items whose signature the host already confirmed. */
export function withoutSigned(ws: string, today: TodayDocument, now = Date.now()): TodayDocument {
  const open = (drops.get(ws) ?? []).filter((d) => now - d.at < DROP_MS && [...today.needs_you, ...today.read_only_open].some((i) => same(i, d)))
  if (open.length) drops.set(ws, open)
  else drops.delete(ws)
  if (!open.length) return today
  const keep = (i: NeedsYouItem) => !open.some((d) => same(i, d))
  return { ...today, needs_you: today.needs_you.filter(keep), read_only_open: today.read_only_open.filter(keep) }
}

const refreshed = ['today', 'ticket', 'tickets', 'board', 'agents', 'addon-decisions', 'events']

/** The host confirmed this signature: drop its item now and refresh what it touches. */
export function signedNow(qc: QueryClient, ws: string | undefined, ticket: string, ref?: string, hash?: string | null) {
  if (!ws) return
  drops.set(ws, [...(drops.get(ws) ?? []), { ticket, ref, hash, at: Date.now() }])
  qc.setQueryData<TodayDocument>(['today', ws], (d) => (d ? withoutSigned(ws, d) : d))
  for (const k of refreshed) void qc.invalidateQueries({ queryKey: [k] })
}

/** The host confirmed this answer: the question shows as answered now, without waiting for the next read of the ticket. */
export async function answeredNow(
  qc: QueryClient,
  ws: string | undefined,
  ticketKey: string,
  questionId: string,
  answer: { option?: string; text?: string },
  by: string,
) {
  if (!ws) return
  const at = new Date().toISOString()
  // A read of the ticket still in flight would overwrite the answer with the old state.
  await qc.cancelQueries({ queryKey: ['ticket', ws, ticketKey] })
  qc.setQueryData<TicketDocument>(['ticket', ws, ticketKey], (d) => {
    if (!d) return d
    const done = (q: QuestionStatus) => ({ ...q, state: 'answered', answer: { by, at, ...(answer.option ? { option: answer.option } : {}), ...(answer.text ? { text: answer.text } : {}) } }) as QuestionStatus
    return { ...d, questions_state: d.questions_state.map((q) => (q.id === questionId ? done(q) : q)) }
  })
}

export function resetSignedDrops() {
  drops.clear()
}
