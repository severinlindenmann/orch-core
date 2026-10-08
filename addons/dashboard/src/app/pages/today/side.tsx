import { Link } from '@tanstack/react-router'
import { Bot, Server, User } from 'lucide-react'
import type { AgentInfo, TicketDocument, TodayDocument } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { ago, displayName, type Directory } from './shared'

export function AgentsAtWork({ agents, tickets, dir, now }: { agents: AgentInfo[]; tickets: Record<string, TicketDocument | undefined>; dir: Directory; now: string }) {
  const rows = agents.flatMap((a) => a.claims.map((c) => ({ a, c })))
  return (
    <section aria-labelledby="agents-h" className="rounded-lg border border-border bg-surface">
      <h2 id="agents-h" className="border-b border-border px-4 py-2.5 text-[13px] font-semibold text-text">
        Agents at work
      </h2>
      {rows.length === 0 ? (
        <p className="px-4 py-3 text-[13px] text-text-muted">No agent holds a ticket right now.</p>
      ) : (
        <ul className="divide-y divide-border">
          {rows.map(({ a, c }) => {
            const t = tickets[c.ticket]
            const doing = t?.tasks_state.find((x) => x.state === 'doing')
            const leases = a.leases.filter((l) => l.ticket === c.ticket)
            return (
              <li key={`${a.id}-${c.ticket}`} className="space-y-1 px-4 py-3">
                <div className="flex items-center gap-2 text-[13px]">
                  <Bot className="size-3.5 text-text-muted" />
                  <span className="font-medium text-text">{a.name}</span>
                  <span className="text-text-muted">for {displayName(dir, a.for)}</span>
                  <span className="ml-auto text-xs tabular-nums text-text-faint">since {ago(c.since, now)}</span>
                </div>
                <Link
                  to="/ticket/$key"
                  params={{ key: c.ticket }}
                  className="block truncate rounded text-[13px] text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <span className="mr-1.5 font-mono text-xs">{c.ticket}</span>
                  {t?.title}
                </Link>
                {doing && (
                  <p className="truncate text-xs text-text-muted">
                    <span className="mr-1.5 font-mono text-text-faint">{doing.id}</span>
                    {doing.text}
                  </p>
                )}
                {leases.length > 0 && (
                  <p className="text-xs tabular-nums text-text-faint">
                    {leases.length} subagent{leases.length === 1 ? '' : 's'}: {leases.map((l) => l.task).join(', ')}
                  </p>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function ActorIcon({ kind }: { kind: string }) {
  if (kind === 'person') return <User className="size-3.5" aria-label="person" />
  if (kind === 'agent') return <Bot className="size-3.5" aria-label="agent" />
  if (kind === 'addon') return <AddonBadge name="addon" className="size-3.5 text-[9px]" />
  return <Server className="size-3.5" aria-label="host" />
}

export function Recently({ recent, now }: { recent: TodayDocument['recent']; now: string }) {
  return (
    <section aria-labelledby="recent-h" className="rounded-lg border border-border bg-surface">
      <h2 id="recent-h" className="border-b border-border px-4 py-2.5 text-[13px] font-semibold text-text">
        Recently
      </h2>
      <ul className="divide-y divide-border">
        {recent.slice(0, 6).map((e) => (
          <li key={`${e.ticket}-${e.seq}`} className="flex items-start gap-2.5 px-4 py-2.5">
            <span className="mt-0.5 text-text-muted">
              <ActorIcon kind={(e.actor as { kind: string }).kind} />
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[13px] leading-snug text-text">{e.summary}</p>
              <Link
                to="/ticket/$key"
                params={{ key: e.ticket }}
                className="rounded font-mono text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
              >
                {e.ticket}
              </Link>
            </div>
            <span className="shrink-0 text-xs tabular-nums text-text-faint">{ago(e.at, now)}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
