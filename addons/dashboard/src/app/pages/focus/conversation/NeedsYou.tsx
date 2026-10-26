import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { Prose } from '@/components/sign/visible'
import { Textarea } from '@/components/ui/textarea'
import { fmtDateTime } from '@/lib/time'
import { SignDialog } from '../../ticket/SignDialog'
import { canAnswer } from '../../ticket/actions'
import { useViewer, type HumanAction } from '../../ticket/shared'

export function NeedsYou({ ws, ticketKey }: { ws: string; ticketKey: string }) {
  const { data: ticket } = useQuery(queries.ticket(ws, ticketKey))
  const viewer = useViewer(ticketKey)
  const [action, setAction] = useState<HumanAction | null>(null)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const open = ticket?.questions_state.filter((q) => q.state === 'open') ?? []
  if (!ticket || !open.length) return null
  return <>
    <section aria-label="Needs you" className="max-h-[40vh] shrink-0 space-y-3 overflow-y-auto border-t border-brand/30 bg-brand-soft px-4 py-3 text-sm">
      <h2 className="font-medium">Needs you</h2>
      {open.map((q) => <article key={q.id} className="space-y-2">
        <Prose className="font-medium">{q.text}</Prose>
        {q.asked_at && <time dateTime={q.asked_at} className="text-xs text-text-muted">{fmtDateTime(q.asked_at)}</time>}
        {q.why && <Prose className="text-xs text-text-muted">{q.why}</Prose>}
        <div className="flex flex-wrap gap-2">
          {q.options?.length ? q.options.map((option) => <Button key={option.key} variant="outline" size="sm" className="h-auto whitespace-normal text-left" disabled={!viewer.ready || !canAnswer(q, viewer)} onClick={() => setAction({ kind: 'answer', question: q.id, option: option.key })}>{option.label}</Button>) : <>
            <Textarea aria-label="Your answer" value={answers[q.id] ?? ''} disabled={!canAnswer(q, viewer)} onChange={(event) => setAnswers({ ...answers, [q.id]: event.target.value })} />
            <Button size="sm" disabled={!viewer.ready || !canAnswer(q, viewer) || !answers[q.id]?.trim()} onClick={() => setAction({ kind: 'answer', question: q.id, text: answers[q.id].trim() })}>Send answer</Button>
          </>}
        </div>
      </article>)}
    </section>
    <SignDialog ticket={ticket} action={action} onClose={() => setAction(null)} />
  </>
}
