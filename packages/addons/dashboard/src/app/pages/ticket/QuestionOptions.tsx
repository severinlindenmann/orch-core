import type { QuestionStatus } from '@/api/types'
import { cn } from '@/lib/utils'
import { Inline, visible } from '@/components/sign/visible'
import { RecommendedTag } from './shared'

/** "Short title: the longer text" as two parts when the label has that shape; otherwise the whole label is the text. */
export function splitLabel(label: string): { title: string | null; text: string } {
  const m = /^(.{2,60}?)(?::| [-–—] )\s+(\S[\s\S]*)$/.exec(label)
  return m ? { title: m[1], text: m[2] } : { title: null, text: label }
}

/**
 * The options of a question as cards: the letter, a short title, the option text, the recommended one marked. A click
 * selects (one radio group, keyboard works as for any radio). Text is the question's own, shown through the visible-string helpers.
 */
export function QuestionOptions({
  q,
  name,
  value,
  onChange,
  idPrefix,
  readOnly = false,
  disabled = false,
}: {
  q: QuestionStatus
  name: string
  value: string | null | undefined
  onChange?: (key: string) => void
  idPrefix: string
  readOnly?: boolean
  /** The answer is being signed (or is): the choice cannot change under the strip. */
  disabled?: boolean
}) {
  const options = q.options ?? []
  return (
    <div role={readOnly ? 'list' : 'radiogroup'} aria-label={readOnly ? `Options for ${q.id}` : `Answer to ${q.id}`} className="grid gap-2">
      {options.map((o) => {
        const { title, text } = splitLabel(o.label)
        const recommended = q.recommended === o.key
        const body = (
          <>
            {o.key.length <= 2 && (
              <span aria-hidden className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border border-border bg-surface-2 font-mono text-[12px] uppercase text-text-muted">
                {o.key}
              </span>
            )}
            <span className="min-w-0 flex-1 space-y-0.5">
              <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span className="text-[13px] font-medium text-text">
                  <Inline>{title ?? text}</Inline>
                </span>
                {recommended && <RecommendedTag />}
                {o.cost && <span className="text-[11px] text-text-muted">{o.cost}</span>}
              </span>
              {title && (
                <span className="block text-[13px] leading-snug text-text-muted">
                  <Inline>{text}</Inline>
                </span>
              )}
            </span>
          </>
        )
        if (readOnly)
          return (
            <div key={o.key} role="listitem" className="flex items-start gap-3 rounded-md border border-border px-3 py-2">
              {body}
            </div>
          )
        return (
          <label
            key={o.key}
            htmlFor={`${idPrefix}-${o.key}`}
            className={cn(
              disabled ? 'opacity-60' : 'cursor-pointer hover:bg-surface-2', 'flex items-start gap-3 rounded-md border px-3 py-2 has-[:checked]:border-brand has-[:checked]:bg-brand-soft has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-brand/40',
              recommended ? 'border-border-strong' : 'border-border',
            )}
          >
            <input type="radio" id={`${idPrefix}-${o.key}`} name={name} value={o.key} checked={value === o.key} disabled={disabled} onChange={() => onChange?.(o.key)} className="sr-only" aria-label={`${o.key.length <= 2 ? `${visible(o.key)}: ` : ''}${visible(o.label)}${recommended ? ', recommended' : ''}${o.cost ? `, ${visible(o.cost)}` : ''}`} />
            {body}
          </label>
        )
      })}
    </div>
  )
}
