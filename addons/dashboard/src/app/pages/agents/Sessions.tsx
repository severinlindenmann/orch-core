import { Link } from '@tanstack/react-router'
import { Bot, Terminal } from 'lucide-react'
import type { AgentSession } from '@/api/types'
import { Pill, Mono, ago } from '../ticket/shared'

const STATE_TONE = { working: 'brand', waiting: 'warning', idle: 'neutral', stopped: 'danger' } as const
const LINK = 'rounded text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring'

function SessionItem({ s, all, viewer, name, now, level }: { s: AgentSession; all: AgentSession[]; viewer: string; name: (id: string) => string; now: number; level: number }) {
  const subs = all.filter((x) => x.parent === s.session)
  const leases = s.leases.filter((l) => l.session === s.session)
  const Icon = s.harness === 'ci' ? Terminal : Bot
  return (
    <li role="treeitem" aria-selected={false} aria-level={level} aria-expanded={subs.length ? true : undefined} aria-labelledby={`${s.session}-name ${s.session}-state`} className="outline-none">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-1.5 text-[13px] hover:bg-surface-2">
        <Icon className="size-3.5 text-text-muted" aria-hidden />
        <span id={`${s.session}-name`} className="flex items-baseline gap-2">
          <span className="font-medium text-text">{s.name}</span>
          <Mono className="text-text-muted">{s.session}</Mono>
        </span>
        {s.model && <Mono className="text-text-faint">{s.model}</Mono>}
        <span id={`${s.session}-state`}>
          <Pill tone={STATE_TONE[s.state]}>{s.state}</Pill>
        </span>
        {s.claims.map((c) => (
          <Link key={c.ticket} to="/ticket/$key" params={{ key: c.ticket }} className={LINK}>
            claims <Mono>{c.ticket}</Mono>
          </Link>
        ))}
        {leases.map((l) => (
          <Link key={`${l.ticket}/${l.task}`} to="/ticket/$key" params={{ key: l.ticket }} className={LINK}>
            lease <Mono>{`${l.ticket}/${l.task}`}</Mono>
          </Link>
        ))}
        {s.state === 'waiting' && s.waiting_on && (
          <Link to="/" className="rounded text-warning outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">
            {s.for === viewer ? 'waiting on you' : `waiting on ${name(s.for)}`}
          </Link>
        )}
        <span className="ml-auto text-xs tabular-nums text-text-faint">last seen {ago(s.last_seen, now)}</span>
      </div>
      {subs.length > 0 && (
        <ul role="group" className="ml-5 border-l border-border pl-2">
          {subs.map((c) => (
            <SessionItem key={c.session} s={c} all={all} viewer={viewer} name={name} now={now} level={level + 1} />
          ))}
        </ul>
      )}
    </li>
  )
}

/** One tree per person: session -> subagents, each with its claim, leases, model, state and last seen. */
export function Sessions({ sessions, viewer, name, now }: { sessions: AgentSession[]; viewer: string; name: (id: string) => string; now: string }) {
  const people = [...new Set(sessions.filter((s) => !s.parent).map((s) => s.for))]
  const at = Date.parse(now)
  if (sessions.length === 0) return <p className="px-4 py-3 text-[13px] text-text-muted">No agent sessions in this workspace.</p>
  return (
    <ul role="tree" aria-label="Sessions" className="space-y-3 p-3">
      {people.map((p) => (
        <li key={p} role="treeitem" aria-selected={false} aria-level={1} aria-expanded aria-labelledby={`person-${p}`} className="outline-none">
          <p id={`person-${p}`} className="px-2 pb-1 text-xs font-semibold uppercase tracking-wide text-text-faint">
            {name(p)}
          </p>
          <ul role="group">
            {sessions
              .filter((s) => !s.parent && s.for === p)
              .map((s) => (
                <SessionItem key={s.session} s={s} all={sessions} viewer={viewer} name={name} now={at} level={2} />
              ))}
          </ul>
        </li>
      ))}
    </ul>
  )
}
