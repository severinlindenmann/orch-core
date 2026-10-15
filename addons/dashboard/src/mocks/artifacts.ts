// The workspace artifact browser (GET /api/workspaces/:ws/artifacts): every artifact of the tickets the viewer can see.
// Visibility is decided here, on the host: a restricted ticket's artifacts never leave the store for other people.
// Items carry no inline content (a busy-day log is over 100 KB); the drawer reads the ticket like the ticket page does.
import type { Actor, Artifact, ArtifactItem, ArtifactPage, ArtifactQuery } from '@/api/types'
import type { MockStore } from './store'

export const PER_PAGE = 48
const MAX_PER = 200
const SINCE_MS: Record<string, number> = { '24h': 86_400_000, '7d': 7 * 86_400_000, '30d': 30 * 86_400_000 }

function byOf(e: { actor: Actor; addon?: unknown }): ArtifactItem['by'] {
  if (typeof e.addon === 'string') return { kind: 'addon', id: e.addon }
  const a = e.actor
  if (a.kind === 'agent') return { kind: 'agent', id: a.id, for: a.for }
  if (a.kind === 'person') return { kind: 'person', id: a.id }
  if (a.kind === 'addon') return { kind: 'addon', id: a.id }
  return { kind: 'host', id: 'orch' }
}

/** Every artifact of every ticket the viewer may see in `wsId`, newest first. */
export function workspaceArtifacts(store: MockStore, wsId: string): ArtifactItem[] {
  const out: ArtifactItem[] = []
  for (const key of store.ticketKeys(wsId)) {
    if (!store.isVisible(key)) continue
    const t = store.ticket(key)
    if (!t) continue
    const events = store.eventsOf(key).filter((e) => e.type === 'artifact.added')
    for (const a of t.artifacts) {
      const e = events.find((x) => x.name === a.name && x.at === a.at)
      const { preview, ...rest } = a as Artifact
      out.push({ ...rest, ticket: key, ticket_title: t.title, by: e ? byOf(e as unknown as { actor: Actor; addon?: unknown }) : { kind: 'host', id: 'orch' }, has_preview: !!preview })
    }
  }
  return out.sort((a, b) => b.at.localeCompare(a.at) || a.ticket.localeCompare(b.ticket) || a.name.localeCompare(b.name))
}

const matchesBy = (i: ArtifactItem, by: string) =>
  by === 'agents' ? i.by.kind === 'agent' : by === 'people' ? i.by.kind === 'person' : i.by.id === by

/** Filter, count and page. Facets are counted over the other filters, so each menu shows what it would find. */
export function listArtifacts(store: MockStore, wsId: string, q: ArtifactQuery): ArtifactPage {
  const all = workspaceArtifacts(store, wsId)
  const now = Date.parse(store.now())
  const text = q.q?.trim().toLowerCase()
  const keep = {
    kind: (i: ArtifactItem) => !q.kind || i.kind === q.kind,
    ticket: (i: ArtifactItem) => !q.ticket || i.ticket === q.ticket,
    by: (i: ArtifactItem) => !q.by || matchesBy(i, q.by),
    since: (i: ArtifactItem) => !q.since || !SINCE_MS[q.since] || now - Date.parse(i.at) <= SINCE_MS[q.since],
    q: (i: ArtifactItem) => !text || `${i.name} ${i.label ?? ''} ${i.ticket} ${i.ticket_title}`.toLowerCase().includes(text),
  }
  const pass = (i: ArtifactItem, skip?: keyof typeof keep) => (Object.keys(keep) as (keyof typeof keep)[]).every((k) => k === skip || keep[k](i))
  const items = all.filter((i) => pass(i))

  const count = <K extends string>(list: ArtifactItem[], key: (i: ArtifactItem) => K) => {
    const m = new Map<K, number>()
    for (const i of list) m.set(key(i), (m.get(key(i)) ?? 0) + 1)
    return m
  }
  const kinds = count(all.filter((i) => pass(i, 'kind')), (i) => i.kind)
  const tickets = count(all.filter((i) => pass(i, 'ticket')), (i) => i.ticket)
  const byList = all.filter((i) => pass(i, 'by'))
  const by = count(byList, (i) => i.by.id)
  const titles = new Map(all.map((i) => [i.ticket, i.ticket_title]))
  const kindOfBy = new Map(all.map((i) => [i.by.id, i.by.kind]))

  const per = Math.min(MAX_PER, Math.max(1, Math.floor(q.per ?? PER_PAGE)))
  const pages = Math.max(1, Math.ceil(items.length / per))
  const page = Math.min(pages, Math.max(1, Math.floor(q.page ?? 1)))
  return {
    items: items.slice((page - 1) * per, page * per),
    total: items.length,
    page,
    pages,
    per,
    facets: {
      kinds: [...kinds].map(([kind, n]) => ({ kind, count: n })).sort((a, b) => b.count - a.count || a.kind.localeCompare(b.kind)),
      tickets: [...tickets].map(([key, n]) => ({ key, title: titles.get(key) ?? key, count: n })).sort((a, b) => a.key.localeCompare(b.key)),
      by: [...by].map(([id, n]) => ({ id, kind: kindOfBy.get(id)!, count: n })).sort((a, b) => b.count - a.count || a.id.localeCompare(b.id)),
    },
  }
}
