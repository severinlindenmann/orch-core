import { useQuery } from '@tanstack/react-query'
import { hubQueries, hubServed, type HubAudienceList, type HubShare } from '@/api/hub'
import type { TicketDocument } from '@/api/types'

/** Names come from the audience read, never from raw user ids. Unknown audiences keep the neutral badge. */
export function peopleShareLabel(ticket: Pick<TicketDocument, 'key' | 'type' | 'labels'>, share?: HubShare, audience?: HubAudienceList): string | null {
  if (!share?.available || !share.tickets.includes(ticket.key)) return null
  const rules = share.rules.filter(r => r.kind === 'ticket' && r.value === ticket.key || r.kind === 'type' && r.value === ticket.type || r.kind === 'tag' && ticket.labels.includes(r.value ?? ''))
  if (!rules.length || rules.some(r => !r.audience) || !audience) return 'Shared'
  const names: string[] = []
  for (const rule of rules) {
    for (const id of rule.audience!.users) {
      const name = audience.people.find(p => p.user_id === id)?.name
      if (!name) return 'Shared'
      names.push(name)
    }
    names.push(...rule.audience!.groups)
  }
  return names.length ? `Shared with ${[...new Set(names)].join(', ')}` : 'Shared'
}

export function usePeopleShare(ws: string | undefined, ticket: TicketDocument, enabled: boolean) {
  const served = !!ws && enabled && hubServed(ws)
  const status = useQuery({ ...hubQueries.status(ws ?? ''), enabled: served })
  const joined = served && !!status.data?.joined && (status.data.mode === 'selective' || status.data.mode === 'full')
  const share = useQuery({ ...hubQueries.share(ws ?? ''), enabled: joined })
  const audience = useQuery({ ...hubQueries.audience(ws ?? ''), enabled: joined && !!share.data?.tickets.includes(ticket.key) })
  return { joined, data: share.data, label: joined ? peopleShareLabel(ticket, share.data, audience.data) : null }
}
