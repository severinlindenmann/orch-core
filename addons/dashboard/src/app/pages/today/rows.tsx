// Today's compact rows: one per open item, 56 px, the ask on the first line, the ticket on the second and one inline
// action on the right. A row expands in place for what the action needs; signing always happens in core's dialogs.
import { useState, type ReactNode } from 'react'
import { Link } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { ChevronDown, FileSearch, HelpCircle, Loader2, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { AddonDecision, GateName, NeedsYouItem, TicketDocument } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { addonEdge } from '@/addon-ui/addonClasses'
import { DecisionSignPrompt, decisionBody } from '@/addon-ui/DecisionSignPrompt'
import { TOUCH_ID_MS } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { toastApiError } from '@/app/toast'
import { useWorkspace } from '@/app/workspace'
import type { HumanAction } from '../ticket/shared'
import { ago } from './shared'

export type Sign = (ticket: string, action: HumanAction) => void

function BlockingChip() {
  return <span className="inline-flex shrink-0 items-center rounded bg-danger-soft px-1.5 py-0.5 text-[11px] font-medium leading-none text-danger">blocking</span>
}

/** The tag beside a recommended option: a word, not a filled button. */
function RecommendedTag() {
  return <span className="rounded border border-border px-1 py-px text-[10px] font-medium uppercase tracking-wide text-text-muted">Recommended</span>
}

const toggleCls =
  'block max-w-full min-w-0 rounded text-left text-[13px] font-medium leading-5 text-text outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring'

/** The row frame shared by core items and addon decisions. */
export function RowShell({
  testId,
  icon,
  ask,
  sub,
  blocking,
  age,
  action,
  expanded,
  onToggle,
  children,
  addon,
}: {
  testId: string
  icon: ReactNode
  ask: string
  sub: ReactNode
  blocking?: boolean
  age?: string
  /** The inline action, or "<name> decides" for people who only read. */
  action: ReactNode
  expanded?: boolean
  /** Without it the ask is plain text (read-only rows). */
  onToggle?: () => void
  children?: ReactNode
  addon?: boolean
}) {
  return (
    <li data-testid={testId} className={cn('border-b border-border last:border-b-0', addon && addonEdge)}>
      <div className="flex min-h-14 items-center gap-3 px-3 py-2">
        <span className="flex w-4 shrink-0 justify-center text-text-muted">{icon}</span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            {onToggle ? (
              <button type="button" aria-expanded={!!expanded} onClick={onToggle} className={cn(toggleCls, expanded ? 'whitespace-normal' : 'truncate')} title={ask}>
                {ask}
              </button>
            ) : (
              <p className="min-w-0 truncate text-[13px] font-medium leading-5 text-text" title={ask}>
                {ask}
              </p>
            )}
            {blocking && <BlockingChip />}
          </div>
          <div className="min-w-0 truncate text-xs leading-5 text-text-muted">{sub}</div>
        </div>
        {age && <span className="shrink-0 text-xs tabular-nums text-text-faint">{age}</span>}
        <div className="flex shrink-0 items-center gap-1">{action}</div>
      </div>
      {expanded && children && <div className="space-y-3 pb-3 pl-10 pr-3">{children}</div>}
    </li>
  )
}

function TicketLine({ ticket, title }: { ticket: string; title?: string }) {
  return (
    <>
      <Link
        to="/ticket/$key"
        params={{ key: ticket }}
        className="mr-1.5 rounded font-mono text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
      >
        {ticket}
      </Link>
      {title}
    </>
  )
}

const Decides = ({ who }: { who: string }) => <span className="text-xs text-text-muted">{who} decides</span>

interface CoreRowProps {
  item: NeedsYouItem
  ticket?: TicketDocument
  now: string
  expanded: boolean
  onToggle: () => void
  sign: Sign
  /** Who asked (questions): shown muted on the second line. */
  askedBy?: string
  /** Set for people who only read: the row has no buttons and names who decides. */
  decider?: string
}

// ------------------------------------------------------------------ question

export function QuestionRow({ item, ticket, now, expanded, onToggle, sign, askedBy, decider }: CoreRowProps) {
  const q = ticket?.questions_state.find((x) => x.id === item.ref)
  const [choice, setChoice] = useState<string | null>(null)
  const id = `question:${item.ticket}:${item.ref}`
  const readOnly = decider !== undefined
  return (
    <RowShell
      testId={`card-${id}`}
      icon={<HelpCircle className="size-4" aria-label="question" />}
      ask={item.text}
      sub={
        <>
          <TicketLine ticket={item.ticket} title={item.title} />
          {askedBy && <span className="text-text-faint"> · Asked by {askedBy}</span>}
        </>
      }
      blocking={item.blocking}
      age={ago(item.since, now)}
      expanded={expanded}
      onToggle={readOnly ? undefined : onToggle}
      action={
        readOnly ? (
          <Decides who={decider} />
        ) : (
          <Button size="sm" variant="outline" aria-expanded={expanded} onClick={onToggle}>
            Answer
          </Button>
        )
      }
    >
      {q?.why && <p className="text-[13px] leading-relaxed text-text-muted">{q.why}</p>}
      {q?.options?.length ? (
        <div className="flex flex-wrap items-end gap-2">
          <div role="radiogroup" aria-label={`Answer to ${item.ref}`} className="flex flex-wrap gap-2">
            {q.options.map((o) => (
              <label
                key={o.key}
                className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-[13px] text-text has-[:checked]:border-brand has-[:checked]:bg-brand-soft"
              >
                <input type="radio" name={`answer-${id}`} value={o.key} checked={choice === o.key} onChange={() => setChoice(o.key)} className="accent-[var(--brand)]" />
                <span>{o.label}</span>
                {o.cost && <span className="text-xs text-text-muted">· {o.cost}</span>}
                {q.recommended === o.key && <RecommendedTag />}
              </label>
            ))}
          </div>
          <Button size="sm" disabled={!choice || !ticket} onClick={() => choice && sign(item.ticket, { kind: 'answer', question: item.ref!, option: choice })}>
            Send answer…
          </Button>
        </div>
      ) : (
        <Button asChild size="sm" variant="outline">
          <Link to="/ticket/$key" params={{ key: item.ticket }}>
            Open the ticket to answer
          </Link>
        </Button>
      )}
    </RowShell>
  )
}

// ------------------------------------------------------------------ approval (requirements / plan)

export function ApprovalRow({ item, ticket, now, expanded, onToggle, sign, decider }: CoreRowProps) {
  const gate = item.ref as Exclude<GateName, 'verify'>
  const readOnly = decider !== undefined
  const covers =
    gate === 'requirements'
      ? `Requirements · ${ticket?.acceptance.length ?? 0} acceptance criteria`
      : `Plan · ${ticket?.tasks.length ?? 0} tasks · ${ticket?.acceptance.length ?? 0} acceptance criteria`
  return (
    <RowShell
      testId={`card-approval:${item.ticket}:${gate}`}
      icon={<ShieldCheck className="size-4" aria-label="approval" />}
      ask={item.text}
      sub={<TicketLine ticket={item.ticket} title={item.title} />}
      blocking={item.blocking}
      age={ago(item.since, now)}
      expanded={expanded}
      onToggle={readOnly ? undefined : onToggle}
      action={
        readOnly ? (
          <Decides who={decider} />
        ) : (
          <Button size="sm" variant="outline" disabled={!ticket} onClick={() => sign(item.ticket, { kind: 'approve', gate })}>
            Review
          </Button>
        )
      }
    >
      <p className="text-[13px] text-text-muted">{covers}</p>
      <Button size="sm" variant="ghost" disabled={!ticket} onClick={() => sign(item.ticket, { kind: 'request_changes', gate })}>
        Request changes…
      </Button>
    </RowShell>
  )
}

// ------------------------------------------------------------------ verdict

export function VerdictRow({ item, ticket, now, expanded, onToggle, sign, decider }: CoreRowProps) {
  const readOnly = decider !== undefined
  const ac = ticket?.acceptance_state ?? []
  const proven = ac.filter((a) => a.state === 'proven').length
  const receipts = ticket?.tasks_state.filter((t) => t.receipt).length ?? 0
  return (
    <RowShell
      testId={`card-verdict:${item.ticket}`}
      icon={<FileSearch className="size-4" aria-label="verdict" />}
      ask={item.text}
      sub={<TicketLine ticket={item.ticket} title={item.title} />}
      blocking={item.blocking}
      age={ago(item.since, now)}
      expanded={expanded}
      onToggle={readOnly ? undefined : onToggle}
      action={
        readOnly ? (
          <Decides who={decider} />
        ) : (
          <>
            <Button asChild size="sm" variant="ghost">
              <Link to="/ticket/$key" params={{ key: item.ticket }}>
                Evidence
              </Link>
            </Button>
            <Button size="sm" variant="outline" disabled={!ticket} onClick={() => sign(item.ticket, { kind: 'verdict' })}>
              Give verdict
            </Button>
          </>
        )
      }
    >
      <p className="text-xs tabular-nums text-text-muted">
        <span className={cn(proven === ac.length && ac.length > 0 ? 'text-success' : 'text-text')}>
          AC {proven}/{ac.length} evidenced
        </span>
        {' · '}
        {receipts} receipts · {ticket?.artifacts.length ?? 0} artifacts
      </p>
    </RowShell>
  )
}

// ------------------------------------------------------------------ addon decisions (rendered and signed by core)

/** Core's flow for one addon decision: core's prompt, presence, then the post with `confirmed`. */
function useDecide(d: AddonDecision) {
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const [signing, setSigning] = useState<AddonDecision['options'][number] | null>(null)
  // From the click until the post resolves the options are off: no second prompt, no second post.
  const [pending, setPending] = useState(false)
  const sign = async (o: AddonDecision['options'][number]) => {
    setSigning(null)
    if (!workspace) return
    setPending(true)
    try {
      await new Promise((r) => setTimeout(r, TOUCH_ID_MS))
      await api.runAddonAction(workspace.id, d.addon, d.action, decisionBody(d, o.key))
      toast.success(`${d.title}: ${o.label}`)
      await qc.invalidateQueries()
    } catch (e) {
      toastApiError(e, 'That did not work.')
    } finally {
      setPending(false)
    }
  }
  const prompt = signing && (
    <DecisionSignPrompt d={d} option={signing} workspacePrefix={workspace?.prefix ?? ''} onClose={() => setSigning(null)} onSign={() => void sign(signing)} />
  )
  return { choose: setSigning, busy: pending || !!signing, pending, prompt }
}

function DecisionBody({ d, readOnly, showQuestion = true }: { d: AddonDecision; readOnly: boolean; showQuestion?: boolean }) {
  const { choose, busy, pending, prompt } = useDecide(d)
  return (
    <>
      {showQuestion && <p className="text-[13px] leading-relaxed text-text">{d.question}</p>}
      {d.detail && <p className="whitespace-pre-line text-[13px] leading-relaxed text-text-muted">{d.detail}</p>}
      {!readOnly && (
        <div className="flex flex-wrap items-center gap-2">
          {d.options.map((o) => (
            <Button key={o.key} size="sm" variant="outline" disabled={busy} aria-busy={pending || undefined} onClick={() => choose(o)}>
              {pending && <Loader2 className="animate-spin" />}
              {o.label}
            </Button>
          ))}
        </div>
      )}
      {prompt}
    </>
  )
}

/**
 * One open addon decision as a core row: the orange A before the ask and the addon hairline on the left, no orange
 * fill. "Decide" opens it in place; each option goes through core's signing prompt. Pages other than Today may use it
 * as is (uncontrolled) to answer a decision where it is shown.
 */
export function DecisionRow({
  d,
  readOnly,
  ticketTitle,
  addonTitle,
  expanded: controlled,
  onToggle,
  decider,
}: {
  d: AddonDecision
  readOnly: boolean
  ticketTitle?: string
  addonTitle?: string
  expanded?: boolean
  onToggle?: () => void
  decider?: string
}) {
  const [own, setOwn] = useState(false)
  const expanded = controlled ?? own
  const toggle = onToggle ?? (() => setOwn((o) => !o))
  return (
    <RowShell
      testId={`card-addon:${d.id}`}
      addon
      icon={<AddonBadge name={addonTitle ?? d.addon} className="size-3.5 text-[9px]" />}
      ask={d.question}
      sub={d.ticket ? <TicketLine ticket={d.ticket} title={ticketTitle ?? d.title} /> : <span>{d.title}</span>}
      expanded={expanded}
      onToggle={readOnly ? undefined : toggle}
      action={
        readOnly ? (
          decider ? <Decides who={decider} /> : null
        ) : (
          <Button size="sm" variant="outline" aria-expanded={expanded} onClick={toggle}>
            Decide
          </Button>
        )
      }
    >
      <DecisionBody d={d} readOnly={readOnly} showQuestion={false} />
    </RowShell>
  )
}

/** Three or more asks of the same addon action, as one row that opens into one sub-row per decision. */
export function FoldRow({
  label,
  decisions,
  addonTitle,
  readOnly,
  expanded,
  onToggle,
  ticketTitle,
  decider,
}: {
  label: string
  decisions: AddonDecision[]
  addonTitle: string
  ticketTitle?: (key: string) => string | undefined
  /** Set for people who only read: the row names who decides. */
  decider?: string
  readOnly: boolean
  expanded: boolean
  onToggle: () => void
}) {
  return (
    <RowShell
      testId={`fold-${decisions[0].addon}-${decisions[0].action}`}
      addon
      icon={<AddonBadge name={addonTitle} className="size-3.5 text-[9px]" />}
      ask={label}
      sub={<span>{decisions.length} open decisions from {addonTitle}</span>}
      expanded={expanded}
      onToggle={readOnly ? undefined : onToggle}
      action={
        readOnly ? (
          decider ? <Decides who={decider} /> : null
        ) : (
          <Button size="sm" variant="outline" aria-expanded={expanded} onClick={onToggle} aria-label={`Review ${decisions.length}`}>
            Review {decisions.length}
            <ChevronDown className={cn('transition-transform', expanded && 'rotate-180')} />
          </Button>
        )
      }
    >
      <ul className="divide-y divide-border rounded-md border border-border">
        {decisions.map((d) => (
          <li key={d.id} data-testid={`card-addon:${d.id}`} className="space-y-2 px-3 py-2.5">
            {d.ticket && (
              <p className="truncate text-xs text-text-muted">
                <TicketLine ticket={d.ticket} title={ticketTitle?.(d.ticket) ?? d.title} />
              </p>
            )}
            <DecisionBody d={d} readOnly={readOnly} />
          </li>
        ))}
      </ul>
    </RowShell>
  )
}
