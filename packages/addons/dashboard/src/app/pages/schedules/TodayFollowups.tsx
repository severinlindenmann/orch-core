// Today's follow-up cards (#720, pick B for failed and did not run, A for the chain cap). A card exists only when a person is
// needed: it did not run, it failed, orch stopped a chain, or the deadline woke the agent without its event. Success never
// makes a card. "Hide for me" hides the card for this person in this browser and changes nothing on the ticket: the ticket
// strip and the board chip keep showing the problem until someone acts.
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { followupsOn, hide, hiddenIds, headline, kindOf, reasonText, todayCards, type FollowupRow } from '@/api/followups'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Pill, RescheduleButton, RunNowButton, StateBadges, CancelButton, useFollowups } from './FollowupParts'

export function useTodayFollowups() {
  const f = useFollowups()
  const [tick, setTick] = useState(0)
  const hidden = f.ws && f.person ? hiddenIds(f.ws, f.person) : new Set<string>()
  void tick
  const cards = f.on && f.data ? todayCards(f.rows, hidden) : []
  return { ...f, cards, hide: (id: string) => { if (f.ws && f.person) { hide(f.ws, f.person, id); setTick((t) => t + 1) } } }
}

function Card({ row, ws, can, onHide }: { row: FollowupRow; ws: string; can: { cancel: boolean; reschedule: boolean; undo: boolean }; onHide: () => void }) {
  const kind = kindOf(row)
  const stopped = kind === 'needs_person'
  const woken = kind === 'woken'
  return (
    <li className="space-y-2 px-3 py-3" data-testid="followup-card" data-followup={row.id} data-kind={kind}>
      <div className="flex flex-wrap items-center gap-1.5">
        <Pill tone="border-brand/40 bg-brand-soft text-brand">Schedules</Pill>
        <StateBadges row={row} />
      </div>
      <h3 className="text-[14px] font-semibold">
        {row.ticket} · {woken ? 'The agent was woken without the event' : stopped ? headline(row) : `Follow-up ${kind === 'failed' ? 'failed' : 'did not run'}`}
      </h3>
      <p className="text-[13px] text-text-muted">{reasonText(row)}</p>
      <div className="flex flex-wrap items-center gap-2">
        {woken ? (
          row.ticket && (
            <Button asChild size="sm">
              <Link to="/ticket/$key" params={{ key: row.ticket }}>
                Open the ticket
              </Link>
            </Button>
          )
        ) : can.cancel ? (
          <>
            <RunNowButton row={row} ws={ws} primary={!stopped} />
            <RescheduleButton row={row} ws={ws} canMove={can.reschedule} />
            {stopped && <CancelButton row={row} ws={ws} canUndo={can.undo} />}
          </>
        ) : null}
        <Button type="button" size="sm" variant="ghost" className={cn('text-text-muted')} onClick={onHide} title="Hides this card for you. The ticket keeps showing the problem.">
          Hide for me
        </Button>
      </div>
    </li>
  )
}

export function FollowupCards({ today }: { today: ReturnType<typeof useTodayFollowups> }) {
  const { ws, cards, can } = today
  if (!ws || !can || cards.length === 0) return null
  return (
    <section aria-label="Follow-ups that need a person" className="overflow-hidden rounded-lg border border-border bg-surface" data-testid="followup-cards">
      <h2 className="px-3 py-2.5 text-[13px]">
        <span className="font-semibold">Follow-ups</span> <span className="tabular-nums text-text-muted">· {cards.length}</span>
      </h2>
      <ul className="divide-y divide-border border-t border-border">
        {cards.map((r) => (
          <Card key={r.id} row={r} ws={ws} can={can} onHide={() => today.hide(r.id)} />
        ))}
      </ul>
    </section>
  )
}

export { followupsOn }
