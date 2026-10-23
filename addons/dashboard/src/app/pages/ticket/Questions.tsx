import { CheckCircle2, CircleHelp, OctagonAlert } from 'lucide-react'
import { useState } from 'react'
import type { QuestionStatus } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { AnswerSend } from '@/components/sign/AnswerSend'
import { Prose } from '@/components/sign/visible'
import { canAnswer } from './actions'
import { ANSWER_MAX, answerableKind, answerOf, QuestionDetails, QuestionOptions, TextAnswer, type Picked } from './QuestionOptions'
import { ago, Mono, Pill, presenceLabel, shortHash, type TabProps, type Viewer } from './shared'

const VIA: Record<string, string> = { cli: 'CLI', dashboard: 'dashboard', phone: 'phone' }

function AnswerForm({ q, sign, ticketKey, onViewQuestion }: { q: QuestionStatus; sign: TabProps['sign']; ticketKey: string; onViewQuestion: (t: string | null) => void }) {
  const kind = answerableKind(q)
  const [pick, setPick] = useState<Picked | null>(null)
  // One box per purpose: a note that goes with a key, the words of "Something else", the answer of a text question.
  const [note, setNote] = useState('')
  const [other, setOther] = useState('')
  const [typed, setTyped] = useState('')
  const [noting, setNoting] = useState(false)
  const [busy, setBusy] = useState(false)
  if (!kind) return <p className="mt-3 border-t border-border pt-3 text-[12px] text-text-faint">This question cannot be shown here in full. Answer it in your terminal.</p>
  const text = kind === 'text' ? typed : pick?.type === 'other' ? other : note
  const option = kind === 'choice' && pick?.type === 'key' ? pick.key : undefined
  const answer = kind === 'text' ? answerOf(null, typed) : pick ? answerOf(pick, text) : null
  const ready = !!answer && Array.from(text.trim()).length <= ANSWER_MAX
  const go = () => answer && sign({ kind: 'answer', question: q.id, option: answer.option, text: answer.text })
  return (
    <form
      className="mt-3 space-y-3 border-t border-border pt-3"
      onSubmit={(e) => {
        e.preventDefault()
        // Only the plain Send answer button submits. In one-click mode there is no such button: an Enter on a radio
        // would otherwise open the two-step dialog behind the strip that already holds a prepared request.
        // `null`: an implicit submission. (A browser without `submitter` says undefined: that still submits.)
        if ((e.nativeEvent as SubmitEvent).submitter === null) return
        if (ready) go()
      }}
    >
      {kind === 'choice' ? (
        <QuestionOptions q={q} name={`answer-${q.id}`} value={pick} onChange={setPick} idPrefix={q.id} otherText={other} onOtherText={setOther} disabled={busy} />
      ) : (
        <>
          <TextAnswer q={q} idPrefix={q.id} value={typed} onChange={setTyped} disabled={busy} />
          <QuestionDetails q={q} />
        </>
      )}
      {kind === 'choice' && pick?.type === 'key' &&
        (noting || !!note ? (
          <div className="space-y-1.5">
            <Label htmlFor={`${q.id}-note`} className="text-[12px] text-text-muted">
              Your note (optional, sent with the answer)
            </Label>
            <Textarea id={`${q.id}-note`} rows={2} value={note} disabled={busy} onChange={(e) => setNote(e.target.value)} />
          </div>
        ) : (
          <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[12px]" disabled={busy} onClick={() => setNoting(true)}>
            Add a note
          </Button>
        ))}
      <AnswerSend
        ticket={ticketKey}
        question={q}
        option={option}
        text={text}
        canSend={ready}
        type="submit"
        fallback={go}
        onViewQuestion={onViewQuestion}
        onBusy={setBusy}
      />
    </form>
  )
}

function QuestionCard({ q, viewer, sign, ticketKey }: { q: QuestionStatus; viewer: Viewer; sign: TabProps['sign']; ticketKey: string }) {
  const [viewText, setViewText] = useState<string | null>(null)
  const open = q.state === 'open'
  const stale = q.state === 'stale'
  const answered = stale ? null : q.answer
  const chosen = q.options?.find((o) => o.key === answered?.option)
  return (
    <li
      id={`question-${q.id}`}
      data-state={q.state}
      className={cn('scroll-mt-4 rounded-lg border bg-surface p-4', open && q.blocking ? 'border-warning/50' : 'border-border')}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Mono className="text-text-muted">{q.id}</Mono>
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
          {q.asked_at ? ` · ${ago(q.asked_at)}` : ''}
        </span>
        {q.hash && (
          <Mono className="ml-auto text-[11px] text-text-faint" >
            hash {shortHash(q.hash, 10)}
          </Mono>
        )}
      </div>
      <Prose className="mt-2 text-[14px] font-medium leading-snug">{viewText ?? q.text}</Prose>
      {q.why && <Prose className="mt-1 text-[13px] text-text-muted">{q.why}</Prose>}

      {!answered && !canAnswer(q, viewer) && (
        <div className="mt-3">
          {q.options && q.options.length > 0 ? <QuestionOptions q={q} name={`view-${q.id}`} value={null} idPrefix={`view-${q.id}`} readOnly /> : <QuestionDetails q={q} />}
        </div>
      )}

      {answered && (
        <div className="mt-3 rounded-md border border-success/30 bg-success-soft px-3 py-2 text-[13px]">
          <p className="font-medium text-text">{chosen ? chosen.label : answered.text}</p>
          {chosen && answered.text && <p className="mt-0.5 text-text-muted">{answered.text}</p>}
          <p className="mt-1 text-[12px] text-text-muted">
            {viewer.name(answered.by)}{answered.at ? ` · ${ago(answered.at)}` : ''}
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
  if (qs.length === 0) return <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[13px] text-text-faint">No questions on this ticket.</p>
  const ordered = [...qs].sort((a, b) => Number(b.state === 'open') - Number(a.state === 'open'))
  return (
    <ul className="space-y-3" aria-label="Questions">
      {ordered.map((q) => (
        <QuestionCard key={q.id} q={q} viewer={viewer} sign={sign} ticketKey={ticket.key} />
      ))}
    </ul>
  )
}
