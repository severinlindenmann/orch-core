import { Link } from '@tanstack/react-router'
import type { AgentActivityItem, AgentSession } from '@/api/types'
import { useSlot } from '@/addon-ui/slots'

const MAX = 5

/** The refusal in plain words: who tried what, and why it did not happen. */
export function refusalSentence(i: AgentActivityItem, who: string): string {
  switch (i.refusal?.code) {
    case 'human_only':
      return `${who} tried to approve a plan. Only people can approve.`
    case 'claim.held':
      return `${who} tried to take ${i.ticket}, but another session already holds it.`
    case 'lease.held':
      return `${who} tried to take a task of ${i.ticket} that another session holds.`
    case 'gate.not_approved':
      return `${who} tried to start ${i.ticket} before its plan was approved.`
    case 'verify.failed':
      return `${who}'s check on ${i.ticket} failed. It can try again.`
    case 'grant.scope':
      return `${who} tried to do something its grant does not cover.`
    default:
      return `${who} was refused on ${i.ticket}: ${i.refusal?.message ?? 'not allowed'}`
  }
}

const pretty = (id: string) => id.replace(/-/g, ' ').replace(/^./, (c) => c.toUpperCase())

/** The last few things agents were refused, in plain words; the full feed lives in the Activity addon. */
export function AgentActivity({ items, sessions }: { items: AgentActivityItem[]; sessions: AgentSession[] }) {
  const nav = useSlot('nav').find((n) => n.addon === 'activity')
  const refusals = items.filter((i) => i.refusal).slice(0, MAX)
  return (
    <section aria-labelledby="refusals-h" className="rounded-lg border border-border bg-surface">
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        <h2 id="refusals-h" className="flex-1 text-[13px] font-semibold text-text">
          Recent refusals
        </h2>
        {nav && (
          <Link to="/addon/$name/$page" params={{ name: nav.addon, page: nav.id }} className="rounded text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
            All activity →
          </Link>
        )}
      </header>
      {refusals.length === 0 ? (
        <p className="px-3 py-3 text-[13px] text-text-muted">No agent has been refused lately.</p>
      ) : (
        <ul className="divide-y divide-border">
          {refusals.map((i, n) => (
            <li key={`${i.ticket}-${i.at}-${n}`} className="flex items-baseline gap-3 px-3 py-2 text-[13px]">
              <span className="w-12 shrink-0 text-xs tabular-nums text-text-faint">{i.at.slice(11, 16)}</span>
              <span className="min-w-0 flex-1 text-text-muted">{refusalSentence(i, sessions.find((s) => s.session === i.session)?.name ?? pretty(i.agent))}</span>
              <Link to="/ticket/$key" params={{ key: i.ticket }} className="shrink-0 rounded font-mono text-xs text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring">
                {i.ticket}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
