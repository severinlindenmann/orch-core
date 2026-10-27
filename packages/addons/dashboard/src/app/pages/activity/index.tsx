// Activity on a real host: who did what, newest first, grouped by day in the viewer's local time (issue #595).
// Read-only. The server writes each sentence and withholds hidden text; restricted tickets never arrive for a viewer
// who cannot see them (the host filters every item). Filters: who, which kind of event, which ticket.
import { useMemo, useState } from 'react'
import { Dropdown } from '@/components/pickers/Dropdown'
import { useInfiniteQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Bot, Cpu, User } from 'lucide-react'
import { ACTIVITY_GROUPS, dayKey, dayLabel, type ActivityGroup, type ActivityRow } from '@/api/activity'
import { findHarness, harnessForAgent } from '@/api/harnesses'
import { hasFeature } from '@/api/features'
import { queries } from '@/api/queries'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { fmtClock, fmtExact } from '@/lib/time'
import { cn } from '@/lib/utils'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useWorkspace } from '@/app/workspace'

const KEY = /^[A-Za-z][A-Za-z0-9]{0,15}-[0-9]{1,9}$/

/** The actor in words: a person's name, "Claude Code for Severin", an add-on's id, or "orch". */
export function actorName(a: ActivityRow['actor']): string {
  if (a.kind === 'person') return a.name || 'Someone'
  if (a.kind === 'agent') {
    // The harness name when known; "orch-cli" and other ids are not names a person knows.
    const label = findHarness(harnessForAgent(a.id))?.label ?? 'An agent'
    return a.for_name ? `${label} for ${a.for_name}` : label
  }
  return a.kind === 'addon' ? a.id : 'orch'
}

function ActorIcon({ kind }: { kind: ActivityRow['actor']['kind'] }) {
  const Icon = kind === 'person' ? User : kind === 'agent' ? Bot : Cpu
  return <Icon className="size-3.5" aria-hidden />
}

function Row({ row }: { row: ActivityRow }) {
  return (
    <li className="flex items-start gap-2.5 px-3 py-2.5" data-testid="activity-row">
      <span className="mt-0.5 text-text-muted">
        <ActorIcon kind={row.actor.kind} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] leading-snug text-text">
          <span className="font-medium">
            <Inline>{actorName(row.actor)}</Inline>
          </span>{' '}
          {row.did}
          {row.ticket && (
            <>
              {' on '}
              <Link to="/ticket/$key" params={{ key: row.ticket }} className="rounded font-mono text-xs text-brand outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">
                {row.ticket}
              </Link>
            </>
          )}
        </p>
        {row.ticket && (row.title || row.title_hidden) && (
          <p className="truncate text-xs text-text-muted">{row.title_hidden ? 'The ticket title has hidden characters, so it is not shown.' : <Inline>{row.title!}</Inline>}</p>
        )}
        {(row.detail || row.detail_hidden) && (
          <p className="mt-0.5 line-clamp-2 text-xs text-text-muted">{row.detail_hidden ? 'The text has hidden characters, so it is not shown.' : <Inline>{row.detail!}</Inline>}</p>
        )}
      </div>
      <time dateTime={row.at} title={fmtExact(row.at)} className="shrink-0 text-xs tabular-nums text-text-faint">
        {fmtClock(row.at)}
      </time>
    </li>
  )
}

export function ActivityPage() {
  usePageHeader('Activity')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const [group, setGroup] = useState<ActivityGroup>('all')
  const [who, setWho] = useState('')
  const [ticketText, setTicketText] = useState('')
  const ticket = KEY.test(ticketText.trim()) ? ticketText.trim().toUpperCase() : ''
  const badTicket = ticketText.trim() !== '' && !ticket
  const served = !!ws && hasFeature('activity', ws)
  const feed = useInfiniteQuery({ ...queries.activity(ws ?? '', { group, ...(who ? { who } : {}), ...(ticket ? { ticket } : {}) }), enabled: served && !badTicket })
  const rows = useMemo(() => feed.data?.pages.flatMap((p) => p.items) ?? [], [feed.data])
  const days = useMemo(() => {
    const out: { key: string; label: string; rows: ActivityRow[] }[] = []
    const now = Date.now()
    for (const r of rows) {
      const key = dayKey(r.at)
      const last = out[out.length - 1]
      if (last?.key === key) last.rows.push(r)
      else out.push({ key, label: dayLabel(r.at, now), rows: [r] })
    }
    return out
  }, [rows])
  if (!workspace || !ws) return null
  const unavailable = !served || feed.data?.pages[0]?.available === false
  if (unavailable) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Activity</h1>
        <p data-testid="activity-unavailable" className="text-[13px] text-text-muted">
          This host does not offer the activity timeline yet, so there is nothing to list here.
        </p>
      </div>
    )
  }
  if (feed.isError && !feed.data) return <LoadFailed what="the activity" onRetry={() => void feed.refetch()} />
  const filtered = group !== 'all' || who !== '' || ticket !== ''
  const people = workspace.members ?? []

  return (
    <div className="w-full max-w-4xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Activity</h1>
      <p className="text-[13px] text-text-muted">Who did what in this workspace, newest first. Times are in your local time. You only see tickets you may open.</p>

      <div role="search" aria-label="Filter the activity" className="flex flex-wrap items-end gap-3 rounded-lg border border-border bg-surface p-3">
        <div className="space-y-1.5">
          <Label htmlFor="activity-who">Who</Label>
          <Dropdown id="activity-who" className="min-w-40" value={who} onChange={setWho} options={[{ value: '', label: 'Everyone' }, { value: 'agents', label: 'Agents only' }, ...people.map((m) => ({ value: m.person, label: `${m.name} and their agents` }))]} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="activity-kind">What</Label>
          <Dropdown id="activity-kind" className="min-w-40" value={group} onChange={(v) => setGroup(v as ActivityGroup)} options={ACTIVITY_GROUPS.map((g) => ({ value: g.id, label: g.label }))} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="activity-ticket">Ticket</Label>
          <Input id="activity-ticket" className="w-36" placeholder={`${workspace.prefix}-0001`} value={ticketText} aria-invalid={badTicket || undefined} onChange={(e) => setTicketText(e.target.value)} />
        </div>
        {filtered && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setGroup('all')
              setWho('')
              setTicketText('')
            }}
          >
            Clear filters
          </Button>
        )}
      </div>
      {badTicket && (
        <p role="alert" className="text-[13px] text-danger">
          A ticket looks like {workspace.prefix}-0001.
        </p>
      )}

      {feed.isPending && !badTicket ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the activity…
        </p>
      ) : rows.length === 0 ? (
        <p data-testid="activity-empty" className="text-[13px] text-text-muted">
          {filtered ? 'Nothing matches these filters.' : 'Nothing has happened in this workspace yet.'}
        </p>
      ) : (
        <div className="space-y-5">
          {days.map((d) => (
            <section key={d.key} aria-label={d.label}>
              <h2 className="mb-1.5 text-[13px] font-semibold text-text">{d.label}</h2>
              <ul className={cn('divide-y divide-border rounded-lg border border-border bg-surface')}>
                {d.rows.map((r) => (
                  <Row key={r.id} row={r} />
                ))}
              </ul>
            </section>
          ))}
          {feed.hasNextPage && (
            <Button variant="outline" size="sm" disabled={feed.isFetchingNextPage} onClick={() => void feed.fetchNextPage()}>
              {feed.isFetchingNextPage ? 'Loading…' : 'Show older'}
            </Button>
          )}
          {feed.isError && (
            <p role="alert" className="text-[13px] text-danger">
              Could not load more. Try again in a moment.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
