import { CheckCircle2, CircleHelp, Fingerprint, OctagonAlert } from 'lucide-react'
import { useState } from 'react'
import type { QuestionStatus } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { canAnswer } from './actions'
import { fmtTime, Mono, Pill, shortHash, type TabProps, type Viewer } from './shared'

const VIA: Record<string, string> = { cli: 'CLI', dashboard: 'dashboard', phone: 'phone' }
const PRESENCE: Record<string, string> = { touchid: 'Touch ID', passkey: 'passkey', password: 'password' }

function AnswerForm({ q, sign }: { q: QuestionStatus; sign: TabProps['sign'] }) {
  const [option, setOption] = useState<string | undefined>(undefined)
  const [text, setText] = useState('')
  const ready = !!option || !!text.trim()
  return (
    <form
      className="mt-3 space-y-3 border-t border-border pt-3"
      onSubmit={(e) => {
        e.preventDefault()
        if (ready) sign({ kind: 'answer', question: q.id, option, text: text.trim() || undefined })
      }}
    >
      {q.options && q.options.length > 0 && (
        <RadioGroup value={option ?? ''} onValueChange={setOption} aria-label={`Options for ${q.id}`} className="gap-1.5">
          {q.options.map((o) => (
            <label
              key={o.key}
              htmlFor={`${q.id}-${o.key}`}
              className="flex cursor-pointer items-center gap-2.5 rounded-md border border-border px-3 py-2 text-[13px] hover:bg-surface-2 has-[[data-state=checked]]:border-brand has-[[data-state=checked]]:bg-brand-soft"
            >
              <RadioGroupItem id={`${q.id}-${o.key}`} value={o.key} />
              <span className="flex-1">{o.label}</span>
              {o.cost && <span className="text-[11px] text-text-muted">{o.cost}</span>}
              {q.recommended === o.key && <Pill tone="brand">recommended</Pill>}
            </label>
          ))}
        </RadioGroup>
      )}
      <div className="space-y-1.5">
        <Label htmlFor={`${q.id}-note`} className="text-[12px] text-text-muted">
          {q.options?.length ? 'Add a note (optional)' : 'Your answer'}
        </Label>
        <Textarea id={`${q.id}-note`} rows={2} value={text} onChange={(e) => setText(e.target.value)} />
      </div>
      <div className="flex items-center justify-end gap-2">
        <Button type="submit" size="sm" disabled={!ready}>
          <Fingerprint />
          Answer {q.id}
        </Button>
      </div>
    </form>
  )
}

function QuestionCard({ q, viewer, sign }: { q: QuestionStatus; viewer: Viewer; sign: TabProps['sign'] }) {
  const open = q.state === 'open'
  const answered = q.answer
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
        ) : (
          <Pill tone="success">
            <CheckCircle2 />
            Answered
          </Pill>
        )}
        <span className="text-[12px] text-text-muted">
          to <span className="text-text">{viewer.name(q.to)}</span> · asked by {viewer.name(q.asked_by)} · {fmtTime(q.asked_at)}
        </span>
        {q.hash && (
          <Mono className="ml-auto text-[11px] text-text-faint" >
            hash {shortHash(q.hash, 10)}
          </Mono>
        )}
      </div>
      <p className="mt-2 text-[14px] font-medium leading-snug">{q.text}</p>
      {q.why && <p className="mt-1 text-[13px] text-text-muted">{q.why}</p>}

      {!answered && q.options && q.options.length > 0 && !canAnswer(q, viewer) && (
        <ul className="mt-3 space-y-1">
          {q.options.map((o) => (
            <li key={o.key} className="flex items-center gap-2 text-[13px] text-text-muted">
              <span className="size-1.5 rounded-full bg-border-strong" aria-hidden />
              {o.label}
              {o.cost && <span className="text-[11px] text-text-faint">{o.cost}</span>}
              {q.recommended === o.key && <Pill tone="brand">recommended</Pill>}
            </li>
          ))}
        </ul>
      )}

      {answered && (
        <div className="mt-3 rounded-md border border-success/30 bg-success-soft px-3 py-2 text-[13px]">
          <p className="font-medium text-text">{chosen ? chosen.label : answered.text}</p>
          {chosen && answered.text && <p className="mt-0.5 text-text-muted">{answered.text}</p>}
          <p className="mt-1 text-[12px] text-text-muted">
            {viewer.name(answered.by)} · {fmtTime(answered.at)} · via {VIA[answered.via ?? 'dashboard']} · {PRESENCE[answered.presence ?? 'touchid']}
          </p>
        </div>
      )}

      {open && canAnswer(q, viewer) && <AnswerForm q={q} sign={sign} />}
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
        <QuestionCard key={q.id} q={q} viewer={viewer} sign={sign} />
      ))}
    </ul>
  )
}
