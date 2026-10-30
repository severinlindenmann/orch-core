import { allChecked, VerdictEvidence, VerdictPreview } from './EvidenceView'
import { ManualAcceptanceReview } from './ManualAcceptance'
// A ticket's human action in hand-off mode (real host): approve, request changes, verdict, answer. The form holds only
// what the person types or picks; once they press the verb, the host prepares the request and the dialog shows the
// host's material, never this page's cached ticket. The old Touch ID simulation stays in SignDialog.tsx for the mock.
import { useRef, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { signedNow } from '@/api/signedDrop'
import type { HandoffAction, HandoffRequest } from '@/api/handoff'
import type { Artifact, GateName, TicketDocument } from '@/api/types'
import { SignPopover } from '@/components/sign/SignPopover'
import { Prose, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { blockBoth, gateActionName } from './actions'
import { useViewer, type HumanAction } from './shared'

function titleOf(a: HumanAction, ticketTitle: string): string {
  switch (a.kind) {
    case 'approve_both':
      return `Approve requirements and plan: ${ticketTitle}`
    case 'answer':
      return `Answer ${a.question}`
    case 'approve':
      return `Approve ${gateActionName(a.gate)}`
    case 'verdict':
      return 'Give verdict'
    case 'request_changes':
      return `Request changes on ${gateActionName(a.gate)}`
  }
}

/** The ONE toast after the signature says the outcome, not the mechanism. */
function doneToastOf(a: HumanAction): string {
  switch (a.kind) {
    case 'approve_both':
      return 'Requirements and plan approved'
    case 'answer':
      return 'Answer sent'
    case 'approve':
      return `${gateActionName(a.gate)} approved`
    case 'verdict':
      return 'Verdict recorded'
    case 'request_changes':
      return 'Changes requested'
  }
}

/** The content the ticket had when the request was prepared, for the "changed since you started" warning only. */
function liveHash(ticket: TicketDocument, a: HumanAction): string | null {
  if (a.kind === 'answer') return ticket.questions_state.find((q) => q.id === a.question)?.hash ?? null
  const gate: GateName = a.kind === 'verdict' ? 'verify' : a.kind === 'approve_both' ? 'requirements' : a.gate
  return ticket.gates[gate].hash ?? null
}

export function HandoffSignDialog({
  ticket,
  action,
  ws,
  onClose,
  onOpenEvidence,
  onOpenChanges,
  onPending,
}: {
  ticket: TicketDocument
  action: HumanAction | null
  ws: string | undefined
  onClose: () => void
  onOpenEvidence?: () => void
  onOpenChanges?: () => void
  onPending?: (pending: boolean) => void
}) {
  const viewer = useViewer(ticket.key)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [text, setText] = useState('')
  // Only a pick is stored; Pass is the default while every requirement is proven (what the list calls "Recommended"). Nothing is
  // sent by this: the person still presses Sign, and the verdict signed is the one shown. A new action starts without a pick.
  // The key names the ticket and the content round (hash) too: the dialog stays mounted across tickets and verify rounds.
  const actionKey = action ? `${ticket.key}|${action.kind}-${action.kind === 'answer' ? action.question : 'gate' in action ? action.gate : ''}|${liveHash(ticket, action) ?? ''}` : ''
  const [pick, setPick] = useState<{ k: string; v: 'pass' | 'fail' } | null>(null)
  if (!action && pick) setPick(null) // closed: the next opening starts without a pick
  const outcome: 'pass' | 'fail' | null = pick && pick.k === actionKey ? pick.v : action?.kind === 'verdict' && allChecked(ticket) ? 'pass' : null
  const setOutcome = (v: 'pass' | 'fail') => setPick({ k: actionKey, v })
  const radio = useRef<HTMLInputElement>(null)
  // The opening press of an approve is used once: it is kept per action object and spent when the review starts,
  // so a remount never re-arms without a new press. Approve never depends on the ambient press flag.
  const press = useRef<{ a: HumanAction | null; left: boolean }>({ a: null, left: false })
  if (press.current.a !== action) press.current = { a: action, left: (action?.kind === 'approve' || action?.kind === 'approve_both') && action.pressed === true }
  // A preview replaces the verdict form in the dialog body; the choice and note stay in this component's state.
  const [preview, setPreview] = useState<{ artifact: Artifact; acId: string } | null>(null)
  if (!action) return null

  const title = titleOf(action, ticket.title)
  const note = text.trim()
  let request: HandoffAction | null = null
  let hint: string | null = null
  let confirmLabel = title
  switch (action.kind) {
    case 'approve':
      request = { action: 'approve', key: ticket.key, gate: action.gate }
      break
    case 'approve_both':
      confirmLabel = 'Approve requirements and plan'
      request = { action: 'approve_both', key: ticket.key, head_seq: ticket.head.seq }
      break
    case 'request_changes':
      confirmLabel = 'Request changes'
      hint = note ? null : 'Say what should change to continue'
      request = { action: 'request_changes', key: ticket.key, gate: action.gate, message: text }
      break
    case 'verdict':
      confirmLabel = outcome === 'pass' ? 'Pass' : outcome === 'fail' ? 'Send back' : 'Give verdict'
      hint = !outcome ? 'Pick Pass or Send back to continue' : outcome === 'fail' && !note ? 'Say what should change to continue' : null
      request = outcome ? { action: 'verdict', key: ticket.key, outcome, ...(note ? { message: text } : {}) } : null
      break
    case 'answer':
      confirmLabel = 'Send answer'
      request = {
        action: 'answer',
        key: ticket.key,
        question: action.question,
        ...(action.option ? { option: action.option } : {}),
        ...(action.text?.trim() ? { message: action.text.trim() } : {}),
      }
      break
  }

  const leaveTo = (open?: () => void) => () => {
    onClose()
    if (open) open()
    else void navigate({ to: '/ticket/$key', params: { key: ticket.key } })
  }
  // The request names the content it is bound to; if the page's ticket moved on since, say so (the signer refuses it).
  const now = liveHash(ticket, action)
  const warning = (r: HandoffRequest) => {
    const bound = action.kind === 'answer' ? r.events[0]?.hash : r.gate_hash
    // The page holds the short form of a hash (12 hex); the request the full `sha256:` form.
    const bare = (h: string) => h.replace(/^sha256:/, '')
    return typeof bound === 'string' && now && !bare(bound).startsWith(bare(now)) && !bare(now).startsWith(bare(bound))
      ? 'This ticket changed after the request was prepared. Your terminal refuses what no longer matches. Cancel and prepare again to sign what is there now.'
      : null
  }

  return (
    <SignPopover
      title={title}
      ws={ws}
      action={request}
      doneToast={doneToastOf(action)}
      confirmLabel={confirmLabel}
      autoStart={action.kind === 'approve' || action.kind === 'approve_both' ? press.current.left : undefined}
      armOnStart
      hint={preview ? 'Go back to the verdict to sign' : hint}
      warning={warning}
      onPending={(pending) => {
        if (pending) press.current.left = false
        onPending?.(pending)
      }}
      onSettled={(phase) => {
        // The host confirmed the signature: the item leaves Today and the approvals now, not at the next read.
        if (phase !== 'done') return
        if (action.kind === 'approve_both') {
          signedNow(qc, ws, ticket.key, 'requirements', ticket.gates.requirements.hash ?? null)
          signedNow(qc, ws, ticket.key, 'plan', ticket.gates.plan.hash ?? null)
          return
        }
        signedNow(qc, ws, ticket.key, action.kind === 'answer' ? action.question : action.kind === 'verdict' ? 'verify' : action.gate, now)
      }}
      onPrepareFailed={() => {
        // The combined card is never a dead end: whatever stopped it, the two single approvals are offered instead.
        if (action.kind !== 'approve_both') return
        blockBoth(ticket.key)
        onClose()
      }}
      onClose={onClose}
      initialFocus={() => (action.kind === 'verdict' ? radio.current : null)}
      key={`${action.kind}-${action.kind === 'answer' ? action.question : 'gate' in action ? action.gate : ''}`}
    >
      {action.kind === 'verdict' && preview && <VerdictPreview ticket={ticket} artifact={preview.artifact} onBack={() => setPreview(null)} />}
      {action.kind === 'verdict' && !preview && (
        <>
          <p className="flex flex-wrap items-center gap-x-3 text-[13px] text-text-muted">
            <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[13px]" onClick={leaveTo(onOpenEvidence)}>
              Open evidence
            </Button>
            <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[13px]" onClick={leaveTo(onOpenChanges)}>
              Open changes
            </Button>
          </p>
          <VerdictEvidence ticket={ticket} onPreview={setPreview} name={viewer.name} />
          <ManualAcceptanceReview ticket={ticket} name={viewer.name} />
          <fieldset className="grid gap-2">
            <legend className="sr-only">Verdict</legend>
            {(
              [
                ['pass', 'Pass', allChecked(ticket) ? "Recommended: the agent's check passed" : 'The evidence is enough'],
                ['fail', 'Send back', 'Something must change'],
              ] as const
            ).map(([r, label, reason], i) => (
              <label key={r} className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-[13px] has-[:checked]:border-brand has-[:checked]:bg-brand-soft">
                <input ref={i === 0 ? radio : undefined} type="radio" name="verdict" value={r} checked={outcome === r} onChange={() => setOutcome(r)} className="accent-[var(--brand)]" />
                <span className="font-medium">{label}</span>
                <span className="text-text-muted">{reason}</span>
              </label>
            ))}
          </fieldset>
        </>
      )}
      {(action.kind === 'request_changes' || (action.kind === 'verdict' && !preview)) && (
        <div className="space-y-1.5">
          <Label htmlFor="sign-text">{action.kind === 'request_changes' || outcome === 'fail' ? 'What should change? (required)' : 'Note (optional)'}</Label>
          <Textarea id="sign-text" value={text} onChange={(e) => setText(e.target.value)} rows={3} />
        </div>
      )}
      {action.kind === 'answer' && (
        <section aria-label="Your answer" className="space-y-1 rounded-md border border-border bg-bg p-3 text-[13px]">
          <p className="text-text-muted">
            Question <Raw>{action.question}</Raw>
          </p>
          {action.option && (
            <p>
              Your answer: option <Raw>{action.option}</Raw>
            </p>
          )}
          {action.text?.trim() && <Prose className="text-text">{action.text.trim()}</Prose>}
        </section>
      )}
    </SignPopover>
  )
}
