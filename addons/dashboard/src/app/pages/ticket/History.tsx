import { useQuery } from '@tanstack/react-query'
import { diffWords } from 'diff'
import { ShieldCheck } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '@/api/client'
import type { BodySections, OrchEvent, TicketDocument } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { sectionTitle } from './Overview'
import { ActorIcon, agentName, fmtTime, Mono, Pill, type TabProps, type Viewer } from './shared'

const CORE_PREFIXES = new Set(['ticket', 'status', 'people', 'claim', 'lease', 'task', 'artifact', 'question', 'gate', 'verdict', 'handoff', 'log', 'section', 'edit', 'projection', 'restore'])
const SIGNED_TYPES = new Set(['gate.approved', 'gate.changes_requested', 'verdict.given', 'question.answered', 'people.set'])

type Who = 'person' | 'agent' | 'host' | 'addon'

export function addonOf(e: OrchEvent): string | null {
  if (e.actor.kind === 'addon') return e.actor.id
  const prefix = e.type.split('.')[0]
  return CORE_PREFIXES.has(prefix) ? null : prefix
}

function whoOf(e: OrchEvent): Who {
  if (addonOf(e)) return 'addon'
  return e.actor.kind
}

export function eventDetail(e: OrchEvent, v: Viewer): string {
  const s = (k: string) => String(e[k] ?? '')
  switch (e.type) {
    case 'ticket.created':
      return `Created in ${s('status')}`
    case 'people.set':
      return `People set: owner ${v.name(e.owner as string)}, ${(e.assignees as string[]).length} assignees, ${(e.reviewers as string[]).length} reviewers`
    case 'status.changed':
      return `Status moved to ${s('to')}`
    case 'claim.taken':
      return `${agentName(e.actor.kind === 'agent' ? e.actor.id : '')} claimed for ${v.name(e.actor.kind === 'agent' ? e.actor.for : '')}, expires ${fmtTime(s('expires'))}`
    case 'claim.released':
      return 'Claim released'
    case 'lease.taken':
      return `Lease on ${s('task')} (${e.actor.kind === 'agent' ? e.actor.session : ''})`
    case 'task.done': {
      const r = e.receipt as { exit: number; ms: number; commit?: string }
      return `${s('task')} done, receipt exit ${r.exit} in ${r.ms} ms${r.commit ? ` at ${r.commit}` : ''}`
    }
    case 'artifact.added':
      return `Artifact ${s('name')} (${s('kind')})${e.ac ? ` for ${s('ac')}` : ''}${e.task ? ` from ${s('task')}` : ''}`
    case 'question.asked':
      return `Asked ${s('question')}`
    case 'question.answered':
      return `Answered ${s('question')}${e.option ? ` with "${s('option')}"` : ''}${e.text ? `: ${s('text')}` : ''}`
    case 'gate.approved':
      return `Approved ${s('gate')}`
    case 'gate.changes_requested':
      return `Requested changes on ${s('gate')}${e.text ? `: ${s('text')}` : ''}`
    case 'gate.invalidated':
      return `${s('gate')} approval invalidated: ${s('reason')}`
    case 'verdict.given':
      return `Verdict ${s('result')}${e.text ? `: ${s('text')}` : ''}`
    case 'handoff.written':
      return `Handoff: ${s('text')}`
    case 'section.edited':
      return `Edited section ${s('section').replace('_', ' ')}`
    case 'log.added':
      return s('text')
    case 'github.pr_linked':
      return `Linked pull request #${s('number')}`
    case 'usage.recorded':
      return `Recorded usage: CHF ${(Number(e.cents) / 100).toFixed(2)}`
    case 'land.queued':
      return `Queued for landing on ${s('target')} (${s('remote')})`
    case 'land.dequeued':
      return `Taken off the landing queue${e.reason ? `: ${s('reason')}` : ''}`
    case 'land.attempt':
      return e.outcome === 'merged'
        ? `Landed on ${s('target')}: candidate ${s('candidate_sha')} merged`
        : e.outcome === 'requeued'
          ? `Landing candidate ${s('candidate_sha')} rebuilt: ${s('target')} moved`
          : `Landing failed (${e.reason === 'conflict' ? 'conflict' : 'red checks'}) on candidate ${s('candidate_sha')}`
    case 'land.resolved':
      return `Landing ${e.kind === 'red_checks' ? 'red checks fixed' : 'conflict resolved'} in ${s('file')}: the approval is void, back to review`
    default:
      return e.type
  }
}

function SigStatus({ e }: { e: OrchEvent }) {
  if (e.actor.kind === 'person' && SIGNED_TYPES.has(e.type)) {
    const ok = e.sig_ok !== false
    return (
      <span className={cn('inline-flex items-center gap-1 text-[11px]', ok ? 'text-success' : 'text-danger')}>
        <ShieldCheck className="size-3" />
        {ok ? 'signed, ok' : 'signature failed'}
      </span>
    )
  }
  return <span className="text-[11px] text-text-faint">host-signed</span>
}

const FILTERS: { value: 'all' | Who; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'person', label: 'People' },
  { value: 'agent', label: 'Agents' },
  { value: 'host', label: 'Host' },
  { value: 'addon', label: 'Addons' },
]

function Timeline({ events, viewer }: { events: OrchEvent[]; viewer: Viewer }) {
  const [filter, setFilter] = useState<'all' | Who>('all')
  const shown = events.filter((e) => filter === 'all' || whoOf(e) === filter).slice().reverse()
  return (
    <div className="space-y-3">
      <ToggleGroup type="single" value={filter} onValueChange={(v) => v && setFilter(v as typeof filter)} variant="outline" size="sm" aria-label="Filter by actor kind">
        {FILTERS.map((f) => (
          <ToggleGroupItem key={f.value} value={f.value} className="px-3 text-[12px]">
            {f.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {shown.length === 0 ? (
        <p className="text-[13px] text-text-faint">No events of this kind.</p>
      ) : (
        <ol className="relative space-y-0 border-l border-border pl-5" aria-label="Events, newest first">
          {shown.map((e) => {
            const who = whoOf(e)
            const addon = addonOf(e)
            return (
              <li key={e.seq} className="relative pb-4 last:pb-0" data-who={who}>
                <span className="absolute -left-[30px] top-0.5 flex size-5 items-center justify-center rounded-full border border-border bg-surface text-text-muted">
                  {addon ? <AddonBadge name={addon} className="size-3.5 text-[9px]" /> : <ActorIcon kind={e.actor.kind === 'addon' ? 'host' : e.actor.kind} />}
                </span>
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px]">
                  <span className="text-[13px] font-medium text-text">{e.actor.kind === 'agent' ? `${agentName(e.actor.id)} for ${viewer.name(e.actor.for)}` : e.actor.kind === 'host' ? 'orch' : e.actor.kind === 'addon' ? e.actor.id : viewer.name(e.actor.id)}</span>
                  <Pill>{e.type}</Pill>
                  <span className="text-text-faint">{fmtTime(e.at)}</span>
                  <Mono className="text-[11px] text-text-faint">seq {e.seq}</Mono>
                  <SigStatus e={e} />
                </div>
                <p className="mt-0.5 whitespace-pre-wrap text-[13px] text-text-muted">{eventDetail(e, viewer)}</p>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

const selectCls = 'h-8 rounded-md border border-input bg-surface-2 px-2 text-[13px] text-text outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50'

export function WordDiff({ from, to }: { from: string; to: string }) {
  const parts = useMemo(() => diffWords(from, to), [from, to])
  const added = parts.filter((p) => p.added).reduce((n, p) => n + (p.value.trim() ? p.value.trim().split(/\s+/).length : 0), 0)
  const removed = parts.filter((p) => p.removed).reduce((n, p) => n + (p.value.trim() ? p.value.trim().split(/\s+/).length : 0), 0)
  return (
    <div className="space-y-2">
      <p className="text-[12px] text-text-muted" data-testid="diff-summary">
        <span className="text-success">+{added} words</span> · <span className="text-danger">−{removed} words</span>
      </p>
      <div className="whitespace-pre-wrap rounded-md border border-border bg-bg p-3 text-[13px] leading-relaxed" data-testid="word-diff">
        {parts.map((p, i) =>
          p.added ? (
            <ins key={i} className="rounded-sm bg-success-soft px-0.5 text-success no-underline">
              {p.value}
            </ins>
          ) : p.removed ? (
            <del key={i} className="rounded-sm bg-danger-soft px-0.5 text-danger decoration-danger/70">
              {p.value}
            </del>
          ) : (
            <span key={i}>{p.value}</span>
          ),
        )}
      </div>
    </div>
  )
}

function Changes({ ticket }: { ticket: TicketDocument }) {
  const history = ticket.section_history ?? {}
  const sections = (Object.keys(history) as (keyof BodySections)[]).filter((k) => (history[k]?.length ?? 0) > 0)
  const withChanges = sections.filter((k) => (history[k]?.length ?? 0) > 1)
  const [section, setSection] = useState<keyof BodySections | undefined>(withChanges.includes('plan') ? 'plan' : (withChanges[0] ?? sections[0]))
  const revs = section ? (history[section] ?? []) : []
  const [fromRev, setFromRev] = useState<number | null>(null)
  const [toRev, setToRev] = useState<number | null>(null)
  const to = toRev ?? revs.at(-1)?.rev ?? 1
  const from = fromRev ?? Math.max(1, to - 1)
  const a = revs.find((r) => r.rev === from)
  const b = revs.find((r) => r.rev === to)
  if (!section) return <p className="text-[13px] text-text-faint">No section history yet.</p>
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <label className="space-y-1 text-[12px] text-text-muted">
          <span className="block">Section</span>
          <select
            className={selectCls}
            value={section}
            onChange={(e) => {
              setSection(e.target.value as keyof BodySections)
              setFromRev(null)
              setToRev(null)
            }}
          >
            {sections.map((k) => (
              <option key={k} value={k}>
                {sectionTitle(k, ticket.type)} ({history[k]!.length} revision{history[k]!.length > 1 ? 's' : ''})
              </option>
            ))}
          </select>
        </label>
        {(['From', 'To'] as const).map((label) => (
          <label key={label} className="space-y-1 text-[12px] text-text-muted">
            <span className="block">{label} revision</span>
            <select className={selectCls} value={label === 'From' ? from : to} onChange={(e) => (label === 'From' ? setFromRev(Number(e.target.value)) : setToRev(Number(e.target.value)))}>
              {revs.map((r) => (
                <option key={r.rev} value={r.rev}>
                  r{r.rev} · {fmtTime(r.at)}
                </option>
              ))}
            </select>
          </label>
        ))}
      </div>
      {a && b ? <WordDiff from={a.text} to={b.text} /> : null}
    </div>
  )
}

export function History({ ticket, viewer }: TabProps) {
  const [view, setView] = useState<'timeline' | 'changes'>('timeline')
  const { data, isLoading, error } = useQuery({ queryKey: ['ticket-events', ticket.key, ticket.head.seq], queryFn: () => api.getEvents(ticket.key) })
  return (
    <div className="space-y-4">
      <ToggleGroup type="single" value={view} onValueChange={(v) => v && setView(v as typeof view)} variant="outline" size="sm" aria-label="History view">
        <ToggleGroupItem value="timeline" className="px-3 text-[12px]">
          Timeline
        </ToggleGroupItem>
        <ToggleGroupItem value="changes" className="px-3 text-[12px]">
          Changes
        </ToggleGroupItem>
      </ToggleGroup>
      {view === 'changes' ? (
        <Changes ticket={ticket} />
      ) : isLoading ? (
        <div className="space-y-3">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : error || !data ? (
        <p role="alert" className="text-[13px] text-danger">
          Could not load the history.
        </p>
      ) : (
        <Timeline events={data} viewer={viewer} />
      )}
    </div>
  )
}
