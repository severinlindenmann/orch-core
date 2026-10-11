// Today's compact rows: one per open item, 56 px, the ask on the first line, the ticket on the second and one inline
// action on the right. A row expands in place for what the action needs; signing always happens in core's dialogs.
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { Link } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { ChevronDown, FileSearch, HelpCircle, Loader2, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { diffstat } from '@/api/gates'
import { ApiError, type AddonDecision, type GateName, type NeedsYouItem, type TicketDocument } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { addonEdge } from '@/addon-ui/addonClasses'
import { ErrorAlert } from '@/addon-ui/ErrorAlert'
import type { ActionError } from '@/addon-ui/useRunAddonAction'
import { DecisionSignPrompt, decisionBody, decisionChanged, decisionToast } from '@/addon-ui/DecisionSignPrompt'
import { useAddons } from '@/addon-ui/slots'
import { TOUCH_ID_MS } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { toastApiError } from '@/app/toast'
import { useWorkspace } from '@/app/workspace'
import type { HumanAction } from '../ticket/shared'
import { ago } from './shared'
import { fmtClock, nowMs, plural } from '@/lib/time'

export const NewItemContext = createContext(false)
export const SigningContext = createContext(false)

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
  /** Without it no icon column is drawn. */
  icon?: ReactNode
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
  const fresh = useContext(NewItemContext)
  const pending = useContext(SigningContext)
  return (
    <li data-testid={testId} aria-busy={pending || undefined} className={cn('border-b border-border last:border-b-0', addon && addonEdge, pending && 'opacity-50')}>
      {pending && <p role="status" className="px-3 pt-2 text-xs text-text-muted">Signing…</p>}
      <fieldset disabled={pending} className="min-w-0">
      <div className="flex min-h-14 items-center gap-3 px-3 py-2">
        {icon && <span className="flex w-4 shrink-0 justify-center text-text-muted">{icon}</span>}
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
            {fresh && <span aria-label="New since your last look" title="New since your last look" className="size-1.5 shrink-0 rounded-full bg-brand" />}
            {blocking && <BlockingChip />}
          </div>
          <div className="min-w-0 truncate text-xs leading-5 text-text-muted">{sub}</div>
        </div>
        {age && <span className="shrink-0 text-xs tabular-nums text-text-faint">{age}</span>}
        <div className="flex shrink-0 items-center gap-1">{action}</div>
      </div>
      {expanded && children && <div className={cn('space-y-3 pb-3 pr-3', icon ? 'pl-10' : 'pl-3')}>{children}</div>}
      </fieldset>
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
        ) : expanded ? null : (
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
                <input type="radio" id={`answer-${id}-${o.key}`} name={`answer-${id}`} value={o.key} checked={choice === o.key} onChange={() => setChoice(o.key)} className="accent-[var(--brand)]" />
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
      : gate === 'code'
        ? `Code review · commit ${ticket?.branch.head ?? ''} · ${ticket ? diffstat(ticket.branch) : ''}`
        : `Plan · ${plural(ticket?.tasks.length ?? 0, 'task')} · ${plural(ticket?.acceptance.length ?? 0, 'acceptance criterion', 'acceptance criteria')}`
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

/** The queries an addon decision can change. */
const DECISION_KEYS = ['today', 'addon-decisions', 'addon-state', 'ticket', 'ticket-events', 'ticket-children', 'tickets', 'board', 'agents', 'workspaces']

/** Core's flow for one addon decision: core's prompt, presence, then the post with `confirmed`. */
export function useDecide(d: AddonDecision, onError?: (e: unknown) => void, onDone?: () => void) {
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const { data: packages } = useAddons()
  // The decision as it was when the person chose an option: the prompt shows it and the post sends it, never a later
  // render (a refresh while the prompt is open must not change what is signed; the host refuses a stale snapshot).
  const [signing, setSigning] = useState<{ o: AddonDecision['options'][number]; d: AddonDecision } | null>(null)
  // From the click until the post resolves the options are off: no second prompt, no second post.
  const [pending, setPending] = useState(false)
  const sign = async ({ o, d: opened }: { o: AddonDecision['options'][number]; d: AddonDecision }) => {
    setSigning(null)
    if (!workspace) return
    setPending(true)
    try {
      await new Promise((r) => setTimeout(r, TOUCH_ID_MS))
      const res = await api.runAddonAction(workspace.id, opened.addon, opened.action, decisionBody(opened, o.key))
      // Core's sentence is the title; the addon's own answer rides below it, labelled as the addon's.
      // The snapshot's addon, like the post: the row may show another decision by now.
      const t = decisionToast(packages?.find((p) => p.name === opened.addon)?.title ?? opened.addon, opened.addon, o.key, res.message)
      toast.success(t.message, { description: t.description })
      onDone?.()
      // A decision can move a ticket, an approval or the addon's own state; nothing else (not settings, relay, skills ...).
      await Promise.all(DECISION_KEYS.map((k) => qc.invalidateQueries({ queryKey: [k] })))
    } catch (e) {
      // The decision moved on the host (closed, changed, stale digest): the cached one must not be signed again. Fetch
      // the decisions anew, so the next choice snapshots the current one (Codex integration review #1).
      if (e instanceof ApiError && e.status === 409) await Promise.all(['addon-decisions', 'today', 'addon-state'].map((k) => qc.invalidateQueries({ queryKey: [k] })))
      if (onError) onError(e)
      else toastApiError(e, 'That did not work.')
    } finally {
      setPending(false)
    }
  }
  const prompt = signing && (
    <DecisionSignPrompt d={signing.d} option={signing.o} changed={decisionChanged(signing.d, d)} workspacePrefix={workspace?.prefix ?? ''} onClose={() => setSigning(null)} onSign={() => void sign(signing)} />
  )
  return { choose: (o: AddonDecision['options'][number]) => setSigning({ o, d }), busy: pending || !!signing, pending, prompt }
}

/** A hold's countdown: core's own line from `hold.until`, outside the signed text, so it ticks while a prompt is open. */
function HoldCountdown({ until }: { until: string }) {
  return (
    <span data-hold-countdown className="block text-[12px] text-text-muted">
      Delivering in {Math.max(0, Math.ceil((Date.parse(until) - nowMs()) / 60_000))} min, at {fmtClock(until)}
    </span>
  )
}

function DecisionBody({ d, readOnly, showQuestion = true, inlineErrors, onPending, blockedReason }: { d: AddonDecision; readOnly: boolean; showQuestion?: boolean; inlineErrors?: boolean; onPending?: (pending: boolean) => void; blockedReason?: string }) {
  const [error, setError] = useState<ActionError | null>(null)
  const { choose, busy, pending, prompt } = useDecide(
    d,
    inlineErrors ? (e) => setError(e instanceof ApiError ? { message: e.message, hint: e.hint } : { message: 'That did not work.' }) : undefined,
    () => setError(null),
  )
  useEffect(() => { onPending?.(pending) }, [pending, onPending])
  return (
    <>
      {pending && showQuestion && <p role="status" className="text-xs text-text-muted">Signing…</p>}
      {showQuestion && <p className="text-[13px] leading-relaxed text-text">{d.question}</p>}
      {/* A hold's countdown is core's, from `hold.until`: outside the signed text, so it can tick while a prompt is open. */}
      {showQuestion && d.hold && <HoldCountdown until={d.hold.until} />}
      {d.detail && <p className="whitespace-pre-line text-[13px] leading-relaxed text-text-muted">{d.detail}</p>}
      {!readOnly && (
        <div className="flex flex-wrap items-center gap-2">
          {d.options.map((o) => (
            <Button key={o.key} size="sm" variant="outline" disabled={busy || (!!blockedReason && !!o.primary)} aria-busy={pending || undefined} onClick={() => choose(o)}>
              {pending && <Loader2 className="animate-spin" />}
              {o.label}
            </Button>
          ))}
          {blockedReason && <span role="status" className="text-[12px] text-text-muted">{blockedReason}</span>}
        </div>
      )}
      {error && <ErrorAlert error={error} onDismiss={() => setError(null)} />}
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
  inlineErrors,
  inline = false,
  blockedReason,
}: {
  d: AddonDecision
  readOnly: boolean
  /** The page shows a refusal under the row (addon pages); Today keeps the sticky toast. */
  inlineErrors?: boolean
  ticketTitle?: string
  addonTitle?: string
  expanded?: boolean
  onToggle?: () => void
  decider?: string
  /** On an addon's own page (already framed with [A]): no second badge or hairline, always open, no Decide button. */
  inline?: boolean
  /** Core says the options cannot be used now (e.g. unsaved edits on the page): they are disabled and this is shown. */
  blockedReason?: string
}) {
  const [own, setOwn] = useState(false)
  const [pending, setPending] = useState(false)
  const expanded = controlled ?? own
  const toggle = onToggle ?? (() => setOwn((o) => !o))
  return (
    <SigningContext.Provider value={pending}>
    <RowShell
      testId={`card-addon:${d.id}`}
      addon={!inline}
      icon={inline ? undefined : <AddonBadge name={addonTitle ?? d.addon} className="size-3.5 text-[9px]" />}
      ask={d.question}
      sub={
        <>
          {d.ticket ? <TicketLine ticket={d.ticket} title={ticketTitle ?? d.title} /> : <span>{d.title}</span>}
          {d.hold && <HoldCountdown until={d.hold.until} />}
        </>
      }
      expanded={inline || expanded}
      onToggle={readOnly || inline ? undefined : toggle}
      action={
        inline && !readOnly ? null : readOnly ? (
          decider ? <Decides who={decider} /> : null
        ) : (
          <Button size="sm" variant="outline" aria-expanded={expanded} onClick={toggle}>
            Decide
          </Button>
        )
      }
    >
      <DecisionBody d={d} readOnly={readOnly} showQuestion={false} inlineErrors={inlineErrors} onPending={setPending} blockedReason={blockedReason} />
    </RowShell>
    </SigningContext.Provider>
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
