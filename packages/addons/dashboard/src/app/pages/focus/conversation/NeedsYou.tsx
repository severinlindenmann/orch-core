import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { Button } from '@/components/ui/button'

/** A one-row reminder. Decisions (the panel) is the only place a question is answered and signed: Review opens it. */
export function NeedsYou({ ws, ticketKey, onReview }: { ws: string; ticketKey: string; onReview?: () => void }) {
  const { data: ticket } = useQuery(queries.ticket(ws, ticketKey))
  const open = ticket?.questions_state.filter((q) => q.state === 'open') ?? []
  if (!ticket || !open.length) return null
  return <section aria-label="Needs you" className="flex min-h-12 shrink-0 items-center gap-3 border-t border-brand/30 bg-brand-soft px-4 py-2 text-sm">
    <h2 className="shrink-0 font-medium">Needs you · {open.length}</h2>
    <p className="min-w-0 flex-1 truncate" title={open[0].text}>{open[0].text}</p>
    {onReview && <Button variant="outline" size="sm" className="h-9 shrink-0" onClick={onReview}>Review</Button>}
  </section>
}
