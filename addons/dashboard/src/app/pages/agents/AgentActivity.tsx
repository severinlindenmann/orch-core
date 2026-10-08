import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import type { AgentActivityItem } from '@/api/types'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { FilterSelect } from '../board/Toolbar'
import { Mono, Pill } from '../ticket/shared'

/** Feed of what agents did, with filters (person, ticket, refusals only). Refusals show their code and the stop rule. */
export function AgentActivity({ items, name }: { items: AgentActivityItem[]; name: (id: string) => string }) {
  const [person, setPerson] = useState('all')
  const [ticket, setTicket] = useState('all')
  const [refusals, setRefusals] = useState(false)
  const shown = items.filter((i) => (person === 'all' || i.for === person) && (ticket === 'all' || i.ticket === ticket) && (!refusals || i.refusal))
  const people = [...new Set(items.map((i) => i.for))].map((p) => ({ value: p, label: name(p) }))
  const tickets = [...new Set(items.map((i) => i.ticket))].sort().map((t) => ({ value: t, label: t }))
  return (
    <div>
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-3 py-2">
        <FilterSelect label="Person" value={person} onChange={setPerson} options={people} />
        <FilterSelect label="Ticket" value={ticket} onChange={setTicket} options={tickets} />
        <div className="flex items-center gap-2">
          <Checkbox id="refusals-only" checked={refusals} onCheckedChange={(v) => setRefusals(v === true)} />
          <Label htmlFor="refusals-only" className="text-[12px] font-normal">
            Refusals only
          </Label>
        </div>
        <span className="ml-auto text-xs tabular-nums text-text-faint">{shown.length} shown</span>
      </div>
      {shown.length === 0 ? (
        <p className="px-4 py-3 text-[13px] text-text-muted">Nothing matches these filters.</p>
      ) : (
        <ul className="divide-y divide-border">
          {shown.map((i, n) => (
            <li key={`${i.ticket}-${i.at}-${n}`} className="flex items-start gap-3 px-3 py-2 text-[13px]">
              <span className="w-12 shrink-0 pt-px text-xs tabular-nums text-text-faint">{i.at.slice(11, 16)}</span>
              <div className="min-w-0 flex-1">
                <p className="text-text">
                  <span className="font-medium">{i.agent}</span> <Mono className="text-text-muted">{i.session}</Mono>
                  <span className="text-text-muted"> for {name(i.for)} · </span>
                  <Link to="/ticket/$key" params={{ key: i.ticket }} className="rounded font-mono text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
                    {i.ticket}
                  </Link>
                </p>
                {i.refusal ? (
                  <p className="mt-0.5 flex flex-wrap items-center gap-2">
                    <Mono className="text-danger">{i.refusal.code}</Mono>
                    <span className="text-text-muted">{i.refusal.message}</span>
                    {i.refusal.stop && <Pill tone="danger">stopped after 3 refusals</Pill>}
                  </p>
                ) : (
                  <p className="mt-0.5 text-text-muted">{i.summary}</p>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
