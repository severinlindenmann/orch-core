import { useMemo } from 'react'
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
import { resolveTicketWidgets, type Segment } from './widgets/parse'
import { WidgetBlock } from './widgets/WidgetBlock'

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

/** A section's prose with its widgets drawn in place (core types inline, templates and pages in the sandboxed frame). */
function SectionBody({ segments, ticket, agentHtml, label, list }: { segments: Segment[]; ticket: TicketDocument; agentHtml: boolean; label: string; list?: boolean }) {
  return (
    <>
      {segments.map((s, i) =>
        s.kind === 'markdown' ? (
          s.text.trim() ? <SafeMarkdown key={i} text={list ? asListItems(s.text) : s.text} /> : null
        ) : (
          // Reset when the block's text changes (another ticket, or a live update that fixed it).
          <ErrorBoundary key={i} resetKey={`${ticket.key}\n${s.block.raw}`} fallback={() => <BlockProblem what="This widget" />}>
            <WidgetBlock block={s.block} ticket={ticket} agentHtml={agentHtml} sectionLabel={label} />
          </ErrorBoundary>
        ),
      )}
    </>
  )
}

/** The handoff comes first, then the written sections in spec order. */
const SHOW_ORDER: SectionKey[] = ['current_state', ...SECTION_ORDER.filter((k) => k !== 'current_state')]

export function Overview({ ticket }: TabProps) {
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
            <SectionBody segments={widgets[key] ?? []} ticket={ticket} agentHtml={agentHtml} label={sectionTitle(key, ticket.type)} list={key === 'requirements'} />
          </section>
        )
      })}
      {missing.length > 0 && <p className="text-[13px] text-text-faint">Not written yet: {missing.map((k) => sectionTitle(k, ticket.type)).join(', ')}</p>}
    </div>
  )
}
