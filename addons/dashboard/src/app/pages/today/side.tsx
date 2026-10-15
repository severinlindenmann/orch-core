import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { Bot, ChevronDown, Server, User } from 'lucide-react'
import type { AgentSession, TicketDocument, TodayDocument } from '@/api/types'
import { AddonBadge, AddonContributionView, useSlot } from '@/addon-ui'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import { groupOf, type Attention } from '../../attention'
import { ago } from './shared'

/** Agent rows the side column shows before "All agents". */
export const AGENT_ROWS = 6
/** Glance items the side column shows before "Show N more". */
export const GLANCE_TILES = 3

interface AgentRowData {
  s: AgentSession
  waiting: boolean
  blocker?: AgentSession['waiting_on']
  state: string
  ticket?: string
  since: string
}

/** Root sessions that are not stopped, the ones waiting on the viewer first, then the longest running. */
function agentRows(sessions: AgentSession[], viewer: string | undefined): AgentRowData[] {
  return sessions
    .filter((s) => !s.parent)
    .map((s) => ({ s, group: groupOf(s, sessions, viewer) }))
    .filter((r) => r.group !== 'stopped')
    .map(({ s, group }) => {
      const blocker = [s, ...sessions.filter(a => a.parent === s.session)].find(a => a.state === 'waiting' && a.for === viewer && a.waiting_on)?.waiting_on
      return { s, waiting: group === 'waiting', blocker, state: group === 'waiting-others' ? 'waiting on others' : group, ticket: blocker?.ticket ?? s.waiting_on?.ticket ?? s.claims[0]?.ticket, since: s.claims[0]?.since ?? s.last_seen }
    })
    .sort((a, b) => Number(b.waiting) - Number(a.waiting) || Date.parse(a.since) - Date.parse(b.since) || (a.s.session < b.s.session ? -1 : 1))
}

export const agentsSummary = (a: Attention['agents']) => `${a.working} working${a.waitingOnYou ? ` · ${a.waitingOnYou} waiting on you` : ''}${a.waitingOnOthers ? ` · ${a.waitingOnOthers} waiting on others` : ''}${a.idle ? ` · ${a.idle} idle` : ''}`

function AgentList({ rows, tickets, now, onOpen }: { onOpen?: () => void; rows: AgentRowData[]; tickets: Record<string, TicketDocument | undefined>; now: string }) {
  if (rows.length === 0) return <p className="px-3 py-3 text-[13px] text-text-muted">No agent is working right now.</p>
  return (
    <ul className="divide-y divide-border">
      {rows.map(({ s, waiting, blocker, state, ticket, since }) => (
        <li key={s.session} className="flex min-h-10 items-center gap-2 px-3 py-1.5">
          <Bot className="size-3.5 shrink-0 text-text-muted" aria-hidden />
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-1.5 text-[13px] leading-4 text-text">
              <span className="truncate font-medium">{s.name}</span>
              {waiting && blocker?.kind === 'question' && blocker.ref ? <Link to="/ticket/$key" params={{ key: blocker.ticket }} hash={`question-${blocker.ref}`} onClick={onOpen} aria-label={`waiting on you · ${blocker.ref}, answer it on ${blocker.ticket}`} className="shrink-0 rounded text-[11px] text-warning underline focus-visible:ring-2 focus-visible:ring-ring">waiting on you · {blocker.ref}</Link> : <span className="shrink-0 text-[11px] text-text-muted">{waiting ? 'waiting on you' : state}</span>}
            </p>
            {ticket && (
              <Link
                to="/ticket/$key"
                params={{ key: ticket }}
                className="block truncate rounded text-xs leading-4 text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="mr-1.5 font-mono">{ticket}</span>
                {tickets[ticket]?.title}
              </Link>
            )}
          </div>
          <span className="shrink-0 text-xs tabular-nums text-text-faint">{ago(since, now)}</span>
        </li>
      ))}
    </ul>
  )
}

const AllAgents = () => (
  <Link to="/agents" className="rounded text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
    All agents →
  </Link>
)

/** Side column (≥ 1280 px): at most six agents, waiting on you first. */
export function AgentsPanel({ sessions, viewer, counts, tickets, now }: { sessions: AgentSession[]; viewer?: string; counts: Attention['agents']; tickets: Record<string, TicketDocument | undefined>; now: string }) {
  const rows = agentRows(sessions, viewer)
  return (
    <section aria-labelledby="agents-h" className="rounded-lg border border-border bg-surface">
      <h2 id="agents-h" className="border-b border-border px-3 py-2.5 text-[13px] text-text">
        <span className="font-semibold">Agents</span> <span className="text-text-muted">· {agentsSummary(counts)}</span>
      </h2>
      <AgentList rows={rows.slice(0, AGENT_ROWS)} tickets={tickets} now={now} />
      <div className="border-t border-border px-3 py-2">
        <AllAgents />
      </div>
    </section>
  )
}

/** One column (< 1280 px): a one-line summary; "Show" opens the agents in a sheet. */
export function AgentsBar({ sessions, viewer, counts, tickets, now }: { sessions: AgentSession[]; viewer?: string; counts: Attention['agents']; tickets: Record<string, TicketDocument | undefined>; now: string }) {
  const [open, setOpen] = useState(false)
  return (
    <section aria-label="Agents" className="flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-1.5 text-[13px]">
      <Bot className="size-4 text-text-muted" aria-hidden />
      <span className="text-text">Agents</span>
      <span className="text-text-muted">· {agentsSummary(counts)}</span>
      <Button size="sm" variant="ghost" className="ml-auto h-7" onClick={() => setOpen(true)} aria-haspopup="dialog">
        Show
      </Button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="gap-0 border-border bg-surface p-0">
          <SheetHeader className="border-b border-border">
            <SheetTitle>Agents</SheetTitle>
            <SheetDescription>{agentsSummary(counts)}</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <AgentList rows={agentRows(sessions, viewer)} tickets={tickets} now={now} onOpen={() => setOpen(false)} />
          </div>
          <div className="border-t border-border px-3 py-3">
            <AllAgents />
          </div>
        </SheetContent>
      </Sheet>
    </section>
  )
}

/** A list contribution's number of items (the one key number of e.g. "PRs needing review"), else nothing. */
function listCount(node: unknown): number | undefined {
  const n = node as { type?: unknown; items?: unknown } | null
  return n && n.type === 'list' && Array.isArray(n.items) ? n.items.length : undefined
}

/**
 * Addon `today.card` contributions as one calm list in the language of the core rows (owner feedback D): neutral
 * borders, no card in a card, one small A in each item's header (the item's one addon marker), the item's title, its
 * one key number and secondary line, and "Open" to the addon's own page. In the side column at most `max` (3) items,
 * the rest behind "Show N more" so Agents stays on the first screen.
 */
export function Glance({ readOnly, max }: { readOnly: boolean; max?: number }) {
  const all = useSlot('today.card')
  const pages = useSlot('nav')
  const [more, setMore] = useState(false)
  if (all.length === 0) return null
  const items = max && !more ? all.slice(0, max) : all
  const hidden = all.length - items.length
  return (
    <section aria-labelledby="glance-h" className="rounded-lg border border-border bg-surface">
      <h2 id="glance-h" className="border-b border-border px-3 py-2.5 text-[13px] font-semibold text-text">
        Glance
      </h2>
      <ul className="divide-y divide-border">
        {items.map((c) => {
          const nav = pages.find((p) => p.addon === c.addon)
          const open = nav ? { name: nav.addon, page: nav.id } : undefined
          const count = c.waiting ? undefined : listCount(c.node)
          return (
            <li key={`${c.addon}/${c.id}`} className="space-y-1 px-3 py-2.5">
              <div className="flex items-center gap-2">
                <AddonBadge name={c.addon} title={c.addonTitle} className="size-3.5 text-[9px]" />
                <h3 className="min-w-0 flex-1 truncate text-[13px] font-medium text-text">
                  {c.title}
                  {count !== undefined && <span className="font-normal text-text-muted"> · {count}</span>}
                </h3>
                {open && (
                  <Link to="/addon/$name/$page" params={open} aria-label={`Open ${c.title}`} className="shrink-0 rounded text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
                    Open
                  </Link>
                )}
              </div>
              <div className="pl-5.5">
                <AddonContributionView c={c} readOnly={readOnly} glance={{ open }} />
              </div>
            </li>
          )
        })}
      </ul>
      {max && all.length > max && (
        <div className="border-t border-border px-3 py-2">
          <button
            type="button"
            onClick={() => setMore(!more)}
            className="rounded text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
          >
            {more ? 'Show fewer' : `Show ${hidden} more`}
          </button>
        </div>
      )}
    </section>
  )
}

function ActorIcon({ kind, id }: { kind: string; id?: string }) {
  if (kind === 'person') return <User className="size-3.5" aria-label="person" />
  if (kind === 'agent') return <Bot className="size-3.5" aria-label="agent" />
  if (kind === 'addon') return <AddonBadge name={id ?? 'addon'} className="size-3.5 text-[9px]" />
  return <Server className="size-3.5" aria-label="host" />
}

/** What happened lately; closed by default. */
export function Recently({ recent, now }: { recent: TodayDocument['recent']; now: string }) {
  const [open, setOpen] = useState(false)
  return (
    <section aria-labelledby="recent-h" className="rounded-lg border border-border bg-surface">
      <h2 className="text-[13px]">
        <button
          id="recent-h"
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-2 px-3 py-2.5 text-left font-semibold text-text outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <ChevronDown className={cn('size-4 text-text-muted transition-transform', !open && '-rotate-90')} aria-hidden />
          Recently
        </button>
      </h2>
      {open && (
        <ul className="divide-y divide-border border-t border-border">
          {recent.slice(0, 6).map((e) => (
            <li key={`${e.ticket}-${e.seq}`} className="flex items-start gap-2.5 px-3 py-2.5">
              <span className="mt-0.5 text-text-muted">
                <ActorIcon kind={e.actor.kind} id={e.actor.id} />
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
      )}
    </section>
  )
}
