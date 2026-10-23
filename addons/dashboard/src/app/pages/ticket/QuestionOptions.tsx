import type { QuestionStatus } from '@/api/types'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { Inline, Prose, visible } from '@/components/sign/visible'
import { RecommendedTag } from './shared'

/** The most a typed answer may hold, in code points (core `validate_answer`). */
export const ANSWER_MAX = 1000

/**
 * What the person picked. Tagged, so the "Something else" card can never be confused with a real option whose key
 * happens to be `other`.
 */
export type Picked = { type: 'key'; key: string } | { type: 'other' }

/** The answer event's fields for a pick and the typed text: a key keeps the text as a note; Other is the text alone. */
export function answerOf(pick: Picked | null, text: string): { option?: string; text?: string } | null {
  const t = text.trim()
  if (!pick) return t ? { text: t } : null
  if (pick.type === 'other') return t ? { text: t } : null
  return { option: pick.key, ...(t ? { text: t } : {}) }
}

/** The kind this page can answer, or null (unknown kind, or a question the host did not describe: shown, never answerable). */
export function answerableKind(q: QuestionStatus): 'choice' | 'text' | null {
  if (q.kind === 'text') return 'text'
  if (q.kind === 'choice' && (q.options?.length ?? 0) >= 2) return 'choice'
  return null
}

const letter = (i: number) => String.fromCharCode(65 + i)
const count = (s: string) => Array.from(s).length

/** The question's extra background, folded: plain text, never markup. */
export function QuestionDetails({ q }: { q: { details?: string } }) {
  if (!q.details) return null
  return (
    <details className="text-[13px]">
      <summary className="cursor-pointer text-[12px] text-text-muted">More detail from the agent</summary>
      <Prose className="mt-1.5 font-sans leading-relaxed text-text-muted">{q.details}</Prose>
    </details>
  )
}

/** A text question: one box, no cards. */
export function TextAnswer({
  q,
  idPrefix,
  value,
  onChange,
  disabled = false,
}: {
  q: QuestionStatus
  idPrefix: string
  value: string
  onChange: (v: string) => void
  disabled?: boolean
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={`${idPrefix}-text`} className="text-[12px] text-text-muted">
        Your answer
      </Label>
      <Textarea id={`${idPrefix}-text`} data-testid={`text-${q.id}`} rows={3} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} />
      <Count n={count(value)} />
    </div>
  )
}

function Count({ n }: { n: number }) {
  return (
    <p className={cn('text-right text-[11px]', n > ANSWER_MAX ? 'text-danger' : 'text-text-faint')} aria-live="off">
      {n} / {ANSWER_MAX}
    </p>
  )
}

/**
 * The options of a choice question as cards: letter (by stored order), title, one line of text, facts, the recommended one
 * marked. "Something else" is a card of its own when the question allows it. A click selects (one radio group, keyboard
 * works as for any radio). Every string is the question's own, shown through the visible-string helpers (no markup).
 * Used the same way in Today, the Questions tab and the dialog.
 */
export function QuestionOptions({
  q,
  name,
  value,
  onChange,
  idPrefix,
  otherText = '',
  onOtherText,
  readOnly = false,
  disabled = false,
}: {
  q: QuestionStatus
  name: string
  value: Picked | null | undefined
  onChange?: (pick: Picked) => void
  idPrefix: string
  /** What is typed on the "Something else" card. */
  otherText?: string
  onOtherText?: (v: string) => void
  readOnly?: boolean
  /** The answer is being signed (or is): the choice cannot change under the strip. */
  disabled?: boolean
}) {
  const options = q.options ?? []
  const other = q.allow_other === true
  const box = (selected: boolean, recommended: boolean, extra?: string) =>
    cn(
      'flex items-start gap-3 rounded-md border px-3 py-2',
      readOnly
        ? 'border-border'
        : cn(
            disabled ? 'opacity-60' : 'cursor-pointer hover:bg-surface-2',
            'has-[:checked]:border-brand has-[:checked]:bg-brand-soft has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-brand/40',
            recommended ? 'border-border-strong' : 'border-border',
          ),
      selected && 'border-brand',
      extra,
    )
  return (
    <div className="space-y-3">
      <div role={readOnly ? 'list' : 'radiogroup'} aria-label={readOnly ? `Options for ${q.id}` : `Answer to ${q.id}`} className="grid gap-2">
        {options.map((o, i) => {
          const recommended = q.recommended === o.key
          const facts = o.facts ?? []
          const body = (
            <>
              <span aria-hidden className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 font-mono text-[12px] text-text-muted">
                {letter(i)}
              </span>
              <span className="min-w-0 flex-1 space-y-0.5">
                <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                  <span className="text-[13px] font-medium text-text">
                    <Inline>{o.label}</Inline>
                  </span>
                  {recommended && <RecommendedTag />}
                  {o.cost && (
                    <span className="text-[11px] text-text-muted">
                      <Inline>{o.cost}</Inline>
                    </span>
                  )}
                </span>
                {o.text && (
                  <span className="block text-[13px] leading-snug text-text-muted">
                    <Inline>{o.text}</Inline>
                  </span>
                )}
                {facts.length > 0 && (
                  <span className="flex flex-wrap gap-x-3 gap-y-0.5 pt-0.5 text-[12px]" data-testid="facts">
                    {facts.map(([l, v], j) => (
                      <span key={j} className="text-text-muted">
                        <Inline>{l}</Inline> <span className="font-medium text-text"><Inline>{v}</Inline></span>
                      </span>
                    ))}
                  </span>
                )}
              </span>
            </>
          )
          if (readOnly)
            return (
              <div key={o.key} role="listitem" className={box(false, recommended)}>
                {body}
              </div>
            )
          const checked = value?.type === 'key' && value.key === o.key
          return (
            <label key={o.key} htmlFor={`${idPrefix}-key-${o.key}`} className={box(checked, recommended)}>
              <input
                type="radio"
                id={`${idPrefix}-key-${o.key}`}
                name={name}
                value={o.key}
                checked={checked}
                disabled={disabled}
                onChange={() => onChange?.({ type: 'key', key: o.key })}
                className="sr-only"
                aria-label={`Option ${letter(i)}: ${visible(o.label)}${recommended ? ', recommended' : ''}${o.cost ? `, ${visible(o.cost)}` : ''}`}
              />
              {body}
            </label>
          )
        })}
        {other &&
          (readOnly ? (
            <div role="listitem" className={box(false, false, 'border-dashed')}>
              <span aria-hidden className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 text-[12px] text-text-muted">…</span>
              <span className="text-[13px] font-medium text-text">Something else</span>
            </div>
          ) : (
            <label htmlFor={`${idPrefix}-other`} className={box(value?.type === 'other', false, 'border-dashed')}>
              <input
                type="radio"
                id={`${idPrefix}-other`}
                name={name}
                value="__other__"
                checked={value?.type === 'other'}
                disabled={disabled}
                onChange={() => onChange?.({ type: 'other' })}
                className="sr-only"
                aria-label="Something else, in your own words"
              />
              <span aria-hidden className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 text-[12px] text-text-muted">…</span>
              <span className="min-w-0 flex-1 space-y-0.5">
                <span className="block text-[13px] font-medium text-text">Something else</span>
                <span className="block text-[13px] text-text-muted">Describe it in your own words.</span>
              </span>
            </label>
          ))}
      </div>
      {other && !readOnly && value?.type === 'other' && (
        <div className="space-y-1.5">
          <Label htmlFor={`${idPrefix}-other-text`} className="text-[12px] text-text-muted">
            Your answer
          </Label>
          <Textarea id={`${idPrefix}-other-text`} data-testid={`other-${q.id}`} rows={3} value={otherText} disabled={disabled} onChange={(e) => onOtherText?.(e.target.value)} />
          <Count n={count(otherText)} />
        </div>
      )}
      <QuestionDetails q={q} />
    </div>
  )
}
