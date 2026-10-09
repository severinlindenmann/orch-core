import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { NotebookPen } from 'lucide-react'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { workspaceOfTicket } from '@/api/workspaces'
import type { BodySections, TicketDocument, TicketType } from '@/api/types'
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { BlockProblem, ErrorBoundary } from '@/components/ErrorBoundary'
import { cn } from '@/lib/utils'
import { ago, Pill, type TabProps } from './shared'
import { resolveTicketWidgets, type Segment, type WidgetSpec } from './widgets/parse'
import { WidgetBlock, type PrototypeOf } from './widgets/WidgetBlock'

type SectionKey = keyof BodySections

const TITLES: Record<SectionKey, string> = {
  summary: 'Summary',
  context: 'Context',
  requirements: 'Requirements',
  out_of_scope: 'Out of scope',
  plan: 'Plan',
  decisions: 'Decisions',
  verification: 'Verification',
  current_state: 'Current state',
}

export const SECTION_ORDER: SectionKey[] = ['summary', 'context', 'requirements', 'out_of_scope', 'plan', 'decisions', 'verification', 'current_state']

type Need = 'yes' | 'optional' | 'no'
// Spec section 4: the prose sections depend on the ticket type.
const NEEDS: Record<TicketType, Record<SectionKey, Need>> = {
  feature: { summary: 'optional', context: 'yes', requirements: 'yes', out_of_scope: 'yes', plan: 'yes', decisions: 'yes', verification: 'yes', current_state: 'yes' },
  bug: { summary: 'optional', context: 'yes', requirements: 'yes', out_of_scope: 'yes', plan: 'yes', decisions: 'yes', verification: 'yes', current_state: 'yes' },
  chore: { summary: 'optional', context: 'optional', requirements: 'yes', out_of_scope: 'no', plan: 'yes', decisions: 'optional', verification: 'no', current_state: 'yes' },
  spike: { summary: 'optional', context: 'yes', requirements: 'yes', out_of_scope: 'no', plan: 'yes', decisions: 'yes', verification: 'yes', current_state: 'yes' },
  epic: { summary: 'yes', context: 'yes', requirements: 'yes', out_of_scope: 'yes', plan: 'no', decisions: 'yes', verification: 'no', current_state: 'yes' },
}

export function sectionTitle(key: SectionKey, type: TicketType): string {
  return key === 'verification' && type === 'spike' ? 'Findings' : TITLES[key]
}

const LIST_MARKER = /^\s*(?:[-*+]|\d+[.)])\s/m

/** Requirements written as plain lines (no list markers) read as one list item per non-empty line. */
export function asListItems(text: string): string {
  if (LIST_MARKER.test(text)) return text
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => `- ${l}`)
    .join('\n')
}

/** How many widgets Current state shows before "N more widgets": the handoff stays readable at a glance. */
export const HANDOFF_WIDGETS = 2

/**
 * A widget that sketches the options of an open question (an option prototype, or a widget titled "Q2: …") is a
 * prototype, not the decision: it names the question it belongs to, which is answered (and signed) in Questions.
 */
export function prototypeQuestion(spec: WidgetSpec | undefined, ticket: Pick<TicketDocument, 'questions_state'>): string | undefined {
  if (!spec) return undefined
  const open = ticket.questions_state.filter((q) => q.state === 'open').map((q) => q.id)
  const titled = spec.title?.match(/^(Q\d+)\s*:/)?.[1]
  const options = spec.widget?.startsWith('option-prototype@')
  if (!titled && !options) return undefined
  const named = titled ?? `${spec.title ?? ''} ${spec.caption ?? ''}`.match(/\b(Q\d+)\b/)?.[1]
  if (named) return open.includes(named) ? named : undefined
  return open.length === 1 ? open[0] : undefined
}

/**
 * A section's prose with its widgets drawn in place (core types inline, templates and pages in the sandboxed frame).
 * `maxWidgets`: widgets after that many wait behind "N more widgets" (prose stays); not mounted until asked for.
 */
function SectionBody({ segments, ticket, agentHtml, label, list, maxWidgets, jump }: { segments: Segment[]; ticket: TicketDocument; agentHtml: boolean; label: string; list?: boolean; maxWidgets?: number; jump: TabProps['jump'] }) {
  const [all, setAll] = useState(false)
  const total = segments.filter((s) => s.kind === 'widget').length
  const limit = maxWidgets !== undefined && !all && total > maxWidgets ? maxWidgets : Infinity
  let seen = 0
  return (
    <>
      {segments.map((s, i) => {
        if (s.kind === 'markdown') return s.text.trim() ? <SafeMarkdown key={i} text={list ? asListItems(s.text) : s.text} /> : null
        if (++seen > limit) return null
        const q = prototypeQuestion(s.block.reason ? undefined : s.block.spec, ticket)
        const prototype: PrototypeOf | undefined = q ? { question: q, answer: () => jump({ tab: 'questions', id: `question-${q}` }) } : undefined
        return (
          // Reset when the block's text changes (another ticket, or a live update that fixed it).
          <ErrorBoundary key={i} resetKey={`${ticket.key}\n${s.block.raw}`} fallback={() => <BlockProblem what="This widget" />}>
            <WidgetBlock block={s.block} ticket={ticket} agentHtml={agentHtml} sectionLabel={label} prototype={prototype} />
          </ErrorBoundary>
        )
      })}
      {maxWidgets !== undefined && total > maxWidgets && (
        <button
          type="button"
          aria-expanded={all}
          onClick={() => setAll(!all)}
          className="mt-1 rounded-md border border-border bg-surface px-2.5 py-1 text-[12px] text-text-muted hover:bg-surface-2 hover:text-text"
        >
          {all ? 'Show fewer widgets' : `${total - maxWidgets} more widget${total - maxWidgets === 1 ? '' : 's'}`}
        </button>
      )}
    </>
  )
}

/** The handoff comes first, then the written sections in spec order. */
const SHOW_ORDER: SectionKey[] = ['current_state', ...SECTION_ORDER.filter((k) => k !== 'current_state')]

export function Overview({ ticket, jump }: TabProps) {
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const agentHtml = addonActive(workspaceOfTicket(ticket.key, workspaces.data ?? []), 'widgets')
  const widgets = useMemo(() => resolveTicketWidgets(ticket.body, { order: SECTION_ORDER, label: (k) => sectionTitle(k as SectionKey, ticket.type) }), [ticket.body, ticket.type])
  const needs = NEEDS[ticket.type]
  const written = (k: SectionKey) => !!ticket.body[k]?.trim()
  const shown = SHOW_ORDER.filter(written)
  const missing = SHOW_ORDER.filter((k) => !written(k) && needs[k] === 'yes')
  const handoffAt = ticket.section_history?.current_state?.at(-1)?.at
  return (
    <div className="space-y-5">
      {shown.map((key) => {
        const handoff = key === 'current_state'
        return (
          <section
            key={key}
            aria-labelledby={`sec-${key}`}
            className={cn(handoff && 'rounded-lg border border-brand/40 bg-brand-soft p-4', !handoff && 'border-b border-border pb-5 last:border-b-0')}
          >
            <div className="mb-1 flex items-center gap-2">
              <h2 id={`sec-${key}`} className="text-[15px] font-semibold text-text">
                {handoff && <NotebookPen className="mr-1.5 inline size-4 text-brand" aria-hidden />}
                {sectionTitle(key, ticket.type)}
              </h2>
              {handoff && <Pill tone="brand">handoff{handoffAt ? ` · ${ago(handoffAt)}` : ''}</Pill>}
            </div>
            <SectionBody
              key={ticket.key}
              segments={widgets[key] ?? []}
              ticket={ticket}
              agentHtml={agentHtml}
              label={sectionTitle(key, ticket.type)}
              list={key === 'requirements'}
              maxWidgets={handoff ? HANDOFF_WIDGETS : undefined}
              jump={jump}
            />
          </section>
        )
      })}
      {missing.length > 0 && <p className="text-[13px] text-text-faint">Not written yet: {missing.map((k) => sectionTitle(k, ticket.type)).join(', ')}</p>}
    </div>
  )
}
