import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { Bot, ChevronRight, CircleDot, Hourglass, Square, Terminal } from 'lucide-react'
import type { AgentSession } from '@/api/types'
import { cn } from '@/lib/utils'
import { groupOf } from '../../attention'
import { ago as since_ } from '../today/shared'
import { Mono, ago } from '../ticket/shared'

const SHOWN = 8
const LINK = 'rounded text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring'
const TOGGLE = 'rounded px-1 text-xs tabular-nums text-text-muted outline-none hover:bg-surface-3 hover:text-text focus-visible:ring-2 focus-visible:ring-ring'

const STATE = {
  working: { Icon: CircleDot, tone: 'text-brand' },
  waiting: { Icon: Hourglass, tone: 'text-warning' },
  idle: { Icon: CircleDot, tone: 'text-text-faint' },
  stopped: { Icon: Square, tone: 'text-danger' },
} as const

export interface SessionContext {
  sessions: AgentSession[]
  viewer: string
  name: (id: string) => string
  now: string
  title: (ticket: string) => string | undefined
}

function StateGlyph({ state }: { state: AgentSession['state'] }) {
  const { Icon, tone } = STATE[state]
  return (
    <span className={cn('inline-flex shrink-0', tone)} title={state}>
      <Icon className="size-3.5" aria-hidden />
      <span className="sr-only">{state}</span>
    </span>
  )
}

const since = (s: AgentSession) => s.claims.map((c) => c.since).sort()[0] ?? s.last_seen
const mainTicket = (s: AgentSession) => s.claims[0]?.ticket ?? s.leases[0]?.ticket

function SubRow({ s, ctx }: { s: AgentSession; ctx: SessionContext }) {
  const leases = s.leases.filter((l) => l.session === s.session)
  return (
    <li className="flex items-center gap-3 px-2 py-1 text-xs text-text-muted">
      <StateGlyph state={s.state} />
      <span className="text-text">{s.name}</span>
      {leases.map((l) => (
        <Link key={`${l.ticket}/${l.task}`} to="/ticket/$key" params={{ key: l.ticket }} className={LINK}>
          <Mono>{`${l.ticket}/${l.task}`}</Mono>
        </Link>
      ))}
      <span className="ml-auto tabular-nums text-text-faint">seen {ago(s.last_seen, Date.parse(ctx.now))}</span>
    </li>
  )
}

function Row({ s, ctx }: { s: AgentSession; ctx: SessionContext }) {
  const [subsOpen, setSubsOpen] = useState(false)
  const [detailOpen, setDetailOpen] = useState(false)
  const subs = ctx.sessions.filter((x) => x.parent === s.session)
  const key = mainTicket(s)
  const Icon = s.harness === 'ci' ? Terminal : Bot
  const tickets = s.claims.length
  const elapsed = since_(since(s), ctx.now)
  const state = groupOf(s, ctx.sessions, ctx.viewer) === 'waiting' ? 'waiting' : s.state
  return (
    <li aria-label={`${s.name} for ${ctx.name(s.for)}, ${state}`} className="rounded-md hover:bg-surface-2">
      <div className="flex h-10 items-center gap-3 px-2 text-[13px]">
        <Icon className="size-3.5 shrink-0 text-text-muted" aria-hidden />
        <span className="shrink-0 font-medium text-text">{s.name}</span>
        <span className="shrink-0 text-text-muted">for {ctx.name(s.for)}</span>
        {key ? (
          <Link to="/ticket/$key" params={{ key }} className={cn(LINK, 'flex min-w-0 flex-1 items-baseline gap-1.5')}>
            <Mono className="shrink-0">{key}</Mono>
            <span className="truncate">{ctx.title(key)}</span>
          </Link>
        ) : (
          <span className="flex-1 text-text-faint">no ticket</span>
        )}
        <span className="shrink-0 text-xs tabular-nums text-text-faint">{elapsed}</span>
        <StateGlyph state={state} />
        {subs.length > 0 && (
          <button type="button" aria-expanded={subsOpen} onClick={() => setSubsOpen((v) => !v)} className={TOGGLE}>
            +{subs.length} subagent{subs.length === 1 ? '' : 's'}
          </button>
        )}
        <button type="button" aria-expanded={detailOpen} onClick={() => setDetailOpen((v) => !v)} className={TOGGLE}>
          {tickets === 0 ? 'details' : `${tickets} ticket${tickets === 1 ? '' : 's'}`}
        </button>
      </div>
      {subsOpen && (
        <ul className="ml-7 border-l border-border pb-1 pl-2">
          {subs.map((c) => (
            <SubRow key={c.session} s={c} ctx={ctx} />
          ))}
        </ul>
      )}
      {detailOpen && (
        <dl className="ml-7 grid grid-cols-[5rem_1fr] gap-x-3 gap-y-1 border-l border-border pb-2 pl-2 text-xs">
          <dt className="text-text-faint">Claims</dt>
          <dd className="flex flex-wrap gap-x-3 text-text-muted">
            {s.claims.length === 0 && 'none'}
            {s.claims.map((c) => (
              <Link key={c.ticket} to="/ticket/$key" params={{ key: c.ticket }} className={LINK}>
                <Mono>{c.ticket}</Mono>
              </Link>
            ))}
          </dd>
          {s.model && (
            <>
              <dt className="text-text-faint">Model</dt>
              <dd>
                <Mono className="text-text-muted">{s.model}</Mono>
              </dd>
            </>
          )}
          <dt className="text-text-faint">Session</dt>
          <dd>
            <Mono className="text-text-muted">{s.session}</Mono>
          </dd>
        </dl>
      )}
    </li>
  )
}

/** Root sessions sorted by person, then by how long they have been going. */
export function sortSessions(list: AgentSession[], ctx: Pick<SessionContext, 'name'>) {
  return [...list].sort((a, b) => ctx.name(a.for).localeCompare(ctx.name(b.for)) || Date.parse(since(a)) - Date.parse(since(b)))
}

/** One state group: a collapsible region with a count, 8 rows and "Show N more". */
export function SessionGroup({ id, title, list, ctx, defaultOpen = true, empty }: { id: string; title: string; list: AgentSession[]; ctx: SessionContext; defaultOpen?: boolean; empty?: string }) {
  const [open, setOpen] = useState(defaultOpen)
  const [all, setAll] = useState(false)
  const sorted = sortSessions(list, ctx)
  const shown = all ? sorted : sorted.slice(0, SHOWN)
  return (
    <section aria-labelledby={id} className="rounded-lg border border-border bg-surface">
      <h2 id={id} className="text-[13px] font-semibold text-text">
        <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-2 px-3 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <ChevronRight className={cn('size-3.5 text-text-muted transition-transform', open && 'rotate-90')} aria-hidden />
          {title} ({list.length})
        </button>
      </h2>
      {open && (
        <div className="border-t border-border p-1">
          {list.length === 0 ? (
            <p className="px-2 py-2 text-[13px] text-text-muted">{empty}</p>
          ) : (
            <ul>
              {shown.map((s) => (
                <Row key={s.session} s={s} ctx={ctx} />
              ))}
            </ul>
          )}
          {!all && sorted.length > SHOWN && (
            <button type="button" onClick={() => setAll(true)} className="mx-2 my-1 rounded text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
              Show {sorted.length - SHOWN} more
            </button>
          )}
        </div>
      )}
    </section>
  )
}
