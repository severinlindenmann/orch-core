import { CheckCircle2, CircleHelp, OctagonAlert } from 'lucide-react'
import { useRef, useState } from 'react'
import type { QuestionStatus } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { AnswerSend } from '@/components/sign/AnswerSend'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Prose } from '@/components/sign/visible'
import { canAnswer } from './actions'
import { QuestionOptions } from './QuestionOptions'
import { Ago, Mono, Pill, presenceLabel, shortHash, type TabProps, type Viewer } from './shared'

const VIA: Record<string, string> = { cli: 'CLI', dashboard: 'dashboard', phone: 'phone' }

function AnswerForm({ q, sign, ticketKey, onViewQuestion }: { q: QuestionStatus; sign: TabProps['sign']; ticketKey: string; onViewQuestion: (t: string | null) => void }) {
  const [option, setOption] = useState<string | undefined>(undefined)
  const [text, setText] = useState('')
  const hasOptions = !!q.options?.length
  const [noting, setNoting] = useState(false)
  const [busy, setBusy] = useState(false)
  const showNote = !hasOptions || noting || !!text
  const ready = !!option || !!text.trim()
  return (
    <form
      className="mt-3 space-y-3 border-t border-border pt-3"
      onSubmit={(e) => {
        // Only a press of Send answer sends (AnswerSend): an Enter in the form prepares nothing and opens nothing (C2).
        e.preventDefault()
      }}
    >
      {q.options && q.options.length > 0 && <QuestionOptions q={q} name={`answer-${q.id}`} value={option} onChange={setOption} idPrefix={q.id} disabled={busy} />}
      {showNote ? (
        <div className="space-y-1.5">
          <Label htmlFor={`${q.id}-note`} className="text-[12px] text-text-muted">
            {hasOptions ? 'Your note (optional, sent with the answer)' : 'Your answer'}
          </Label>
          <Textarea id={`${q.id}-note`} rows={2} value={text} disabled={busy} onChange={(e) => setText(e.target.value)} />
        </div>
      ) : (
        <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[12px]" disabled={busy} onClick={() => setNoting(true)}>
          Add a note
        </Button>
      )}
      <AnswerSend
        ticket={ticketKey}
        question={q}
        option={option}
        text={text}
        canSend={ready}
        fallback={() => sign({ kind: 'answer', question: q.id, option, text: text.trim() || undefined })}
        onViewQuestion={onViewQuestion}
        onBusy={setBusy}
      />
    </form>
  )
}

function QuestionCard({ q, viewer, sign, ticketKey, agentLive, isNew }: { q: QuestionStatus; viewer: Viewer; sign: TabProps['sign']; ticketKey: string; agentLive: boolean; isNew: boolean }) {
  const [viewText, setViewText] = useState<string | null>(null)
  const open = q.state === 'open'
  const stale = q.state === 'stale'
  const answered = stale ? null : q.answer
  const chosen = q.options?.find((o) => o.key === answered?.option)
  return (
    <li
      id={`question-${q.id}`}
      data-state={q.state}
      className={cn(
        'scroll-mt-4 rounded-lg border bg-surface p-4',
        open && q.blocking ? 'border-warning/50' : 'border-border',
        // A question that arrived after the page opened slides in and pulses once.
        open && isNew && 'needs-new',
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        {open ? (
          <Pill tone={q.blocking ? 'warning' : 'info'}>
            {q.blocking ? <OctagonAlert /> : <CircleHelp />}
            {q.blocking ? 'Open, blocking' : 'Open'}
          </Pill>
        ) : stale ? (
          <Pill tone="warning">
            <OctagonAlert />
            Changed since it was answered
          </Pill>
        ) : (
          <Pill tone="success">
            <CheckCircle2 />
            Answered
          </Pill>
        )}
        <span className="text-[12px] text-text-muted">
          to <span className="text-text">{viewer.name(q.to)}</span>
          {q.asked_by ? ` · asked by ${viewer.name(q.asked_by)}` : ''}
          {q.asked_at ? <> · <Ago at={q.asked_at} /></> : null}
        </span>
        {/* Plain words on the card (owner rule, run-13): the ids only behind the small "i". */}
        <InfoDrawer className="ml-auto">
          <p>
            Ticket <Mono>{ticketKey}</Mono> · question <Mono>{q.id}</Mono>
          </p>
          {q.hash && <p>Question hash <Mono>{shortHash(q.hash, 10)}</Mono></p>}
        </InfoDrawer>
      </div>
      <Prose className="mt-2 text-[14px] font-medium leading-snug">{viewText ?? q.text}</Prose>
      {q.why && <Prose className="mt-1 text-[13px] text-text-muted">{q.why}</Prose>}

      {!answered && q.options && q.options.length > 0 && !canAnswer(q, viewer) && (
        <div className="mt-3">
          <QuestionOptions q={q} name={`view-${q.id}`} value={null} idPrefix={`view-${q.id}`} readOnly />
        </div>
      )}

      {answered && (
        <div className="mt-3 rounded-md border border-success/30 bg-success-soft px-3 py-2 text-[13px]">
          <p className="font-medium text-text" data-testid="answered-line">
            {answered.by === viewer.person ? 'You answered' : 'Answered'}: {chosen ? chosen.label : answered.text}
            {agentLive && ' · the agent continues'}
          </p>
          {chosen && answered.text && <p className="mt-0.5 text-text-muted">{answered.text}</p>}
          <p className="mt-1 text-[12px] text-text-muted">
            {viewer.name(answered.by)}{answered.at ? <> · <Ago at={answered.at} /></> : null}
            {/* Only a host that says how it came in: nothing is made up for one that does not. */}
            {answered.via ? ` · via ${VIA[answered.via]}` : ''}
            {answered.presence ? ` · ${presenceLabel(answered.presence)}` : ''}
          </p>
        </div>
      )}

      {open && canAnswer(q, viewer) && <AnswerForm q={q} sign={sign} ticketKey={ticketKey} onViewQuestion={setViewText} />}
      {open && !canAnswer(q, viewer) && (
        <p className="mt-3 text-[12px] text-text-faint">Waiting for {viewer.name(q.to)}. The first valid signed answer wins.</p>
      )}
    </li>
  )
}

export function Questions({ ticket, viewer, sign }: TabProps) {
  const qs = ticket.questions_state
  // The questions that were here when the page opened: any other one is new.
  const first = useRef<Set<string>>(new Set(qs.map((q) => q.id)))
  if (qs.length === 0) return <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[13px] text-text-faint">No questions on this ticket.</p>
  const ordered = [...qs].sort((a, b) => Number(b.state === 'open') - Number(a.state === 'open'))
  return (
    <ul className="space-y-3" aria-label="Questions">
      {ordered.map((q) => (
        <QuestionCard key={q.id} q={q} viewer={viewer} sign={sign} ticketKey={ticket.key} agentLive={!!ticket.claim && ticket.claim.live !== false} isNew={!first.current.has(q.id)} />
      ))}
    </ul>
  )
}
