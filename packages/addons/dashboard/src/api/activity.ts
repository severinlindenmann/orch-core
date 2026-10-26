// The Activity page of a real host: `activity.list` as plain-word rows, from the dashboard server's one closed read.
// The server composes the sentence and withholds hidden text; a row carries no host ids beyond the ticket key.

export const ACTIVITY_GROUPS = [
  { id: 'all', label: 'Everything' },
  { id: 'tickets', label: 'Tickets' },
  { id: 'work', label: 'Work' },
  { id: 'talk', label: 'Notes and questions' },
  { id: 'reviews', label: 'Reviews' },
  { id: 'files', label: 'Files' },
  { id: 'workspace', label: 'Workspace' },
] as const
export type ActivityGroup = (typeof ACTIVITY_GROUPS)[number]['id']

export interface ActivityActor {
  kind: 'person' | 'agent' | 'host' | 'addon'
  id: string
  /** A person's name (or "A former member"). */
  name?: string
  /** The person an agent works for. */
  for_name?: string | null
}
export interface ActivityRow {
  id: string
  at: string
  type: string
  group: string
  actor: ActivityActor
  /** What happened, after the actor's name ("moved the ticket from open to done"). */
  did: string
  ticket: string
  title: string | null
  title_hidden: boolean
  detail?: string | null
  detail_hidden?: boolean
}
export interface ActivityPage {
  available: boolean
  items: ActivityRow[]
  next_page: string | null
}
export interface ActivityParams {
  group?: ActivityGroup
  /** 'agents' or a person id. */
  who?: string
  ticket?: string
  limit?: number
  page?: string
}

export function activityQuery(p: ActivityParams): string {
  const q = new URLSearchParams()
  if (p.group && p.group !== 'all') q.set('group', p.group)
  if (p.who) q.set('who', p.who)
  if (p.ticket) q.set('ticket', p.ticket)
  q.set('limit', String(p.limit ?? 50))
  if (p.page) q.set('page', p.page)
  return q.toString()
}

/** "Fri 9 Oct" in the browser's zone, with Today / Yesterday for the last two days. */
export function dayLabel(iso: string, now: number): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const day = (t: Date) => new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime()
  const diff = Math.round((day(new Date(now)) - day(d)) / 86_400_000)
  const text = d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' })
  return diff === 0 ? `Today · ${text}` : diff === 1 ? `Yesterday · ${text}` : text
}

/** The local calendar day of an instant: rows with the same key sit under one heading. */
export function dayKey(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
}
