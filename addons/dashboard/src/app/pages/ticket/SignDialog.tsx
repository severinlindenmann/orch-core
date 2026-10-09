import { useQuery, useQueryClient } from '@tanstack/react-query'
import { workspaceOfTicket } from '@/api/workspaces'
import { useNavigate } from '@tanstack/react-router'
import { Fingerprint, Loader2, ShieldCheck } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, type ActionRequest, type GateName, type TicketDocument } from '@/api/types'
import { ConfirmHelper, SignDetails, TOUCH_ID_MS } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { gateSignedContent, type SignedSection } from '@/api/gates'
import { GATE_LABEL, policyText } from './actions'
import type { HumanAction } from './shared'

type Described = { title: string; gate?: GateName; hash: string; covers: string[]; policy?: string }

/** The dialog title and what the signature covers, in core's words. */
function describe(ticket: TicketDocument, a: HumanAction): Described {
  if (a.kind === 'answer') {
    const q = ticket.questions_state.find((x) => x.id === a.question)!
    const picked = q.options?.find((o) => o.key === a.option)
    return {
      title: `Answer ${q.id}`,
      hash: q.hash ?? '',
      covers: [`Question ${q.id}: ${q.text}`, picked ? `Your answer: ${picked.label}` : 'Your answer: free text', 'The current hash of the question'],
    }
  }
  const gate: GateName = a.kind === 'verdict' ? 'verify' : a.gate
  const g = ticket.gates[gate]
  const name = GATE_LABEL[gate].toLowerCase()
  const title = a.kind === 'approve' ? `Approve ${name}` : a.kind === 'verdict' ? 'Give a verdict' : `Request changes on ${name}`
  return { title, gate, hash: g.hash ?? '', covers: g.covers ?? [], policy: policyText(gate, g) }
}

/** What an approval signs, from the same fields the gate hash covers (api/gates.ts). Type and size are shown but never count as content. */
function signedSections(ticket: TicketDocument, gate: GateName, personName: (id: string) => string): SignedSection[] | null {
  return gate === 'verify' ? null : gateSignedContent(gate, ticket, personName).sections
}

const written = (sections: SignedSection[]) => sections.filter((s) => s.text && !s.meta)

function Signed({ sections }: { sections: SignedSection[] }) {
  return (
    <div className="max-h-[40vh] space-y-3 overflow-auto rounded-md border border-border bg-bg p-3 text-[13px]">
      {sections
        .filter((s) => s.text)
        .map((s) => (
          <section key={s.label} aria-label={s.label}>
            <h3 className="mb-1 text-[12px] font-medium text-text-muted">{s.label}</h3>
            <p className="whitespace-pre-wrap break-words text-text">{s.text}</p>
          </section>
        ))}
    </div>
  )
}

/**
 * Core's signing dialog for a ticket action. It shows what is signed (the text and the list), names the verb on the
 * button, keeps the hash in a closed Details, and starts with focus on Cancel (the first radio for a verdict).
 * `onOpenEvidence` lets the ticket page jump to the evidence; without it the dialog closes and opens the ticket.
 */
export function SignDialog({ ticket, action, onClose, onOpenEvidence }: { ticket: TicketDocument; action: HumanAction | null; onClose: () => void; onOpenEvidence?: () => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [phase, setPhase] = useState<'confirm' | 'touch' | 'sending'>('confirm')
  const [text, setText] = useState('')
  const [result, setResult] = useState<'pass' | 'fail' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const members = workspaceOfTicket(ticket.key, workspaces.data ?? [])?.members ?? []
  const personName = (id: string) => members.find((m) => m.person === id)?.name ?? id
  const cancel = useRef<HTMLButtonElement>(null)
  const firstRadio = useRef<HTMLInputElement>(null)

  useEffect(() => {
    setPhase('confirm')
    setText('')
    setResult(null)
    setError(null)
  }, [action])

  if (!action) return null
  const d = describe(ticket, action)
  const sections = action.kind === 'approve' || action.kind === 'request_changes' ? (d.gate ? signedSections(ticket, d.gate, personName) : null) : null
  const hasContent = !!sections && written(sections).length > 0
  const nothing = action.kind === 'approve' && !!sections && !hasContent
  const needsText = action.kind === 'request_changes' || (action.kind === 'verdict' && result === 'fail')
  const busy = phase !== 'confirm'
  const verb =
    action.kind === 'answer' ? 'Send answer' : action.kind === 'approve' ? d.title : action.kind === 'request_changes' ? 'Request changes' : result === 'pass' ? 'Pass' : result === 'fail' ? 'Send back' : 'Give verdict'
  const hint = nothing
    ? d.gate === 'requirements'
      ? 'Nothing to approve yet: the requirements have no text or acceptance criteria.'
      : 'Nothing to approve yet: the plan has no text or tasks.'
    : action.kind === 'verdict' && !result
      ? 'Choose Pass or Send back'
      : needsText && !text.trim()
        ? 'Say what should change to continue'
        : null
  const blocked = busy || !!hint

  const proven = ticket.acceptance_state.filter((a) => a.state === 'proven').length
  const receipts = ticket.tasks_state.filter((t) => t.receipt).length

  const request = (): ActionRequest => {
    switch (action.kind) {
      case 'approve':
        return { action: 'approve', gate: action.gate }
      case 'request_changes':
        return { action: 'request_changes', gate: action.gate, text }
      case 'verdict':
        return { action: 'verdict', result: result!, text: text || undefined }
      case 'answer':
        return { action: 'answer', question: action.question, option: action.option, text: action.text }
    }
  }

  const run = async () => {
    if (blocked) return
    setError(null)
    setPhase('touch')
    await new Promise((r) => setTimeout(r, TOUCH_ID_MS))
    setPhase('sending')
    try {
      await api.postAction(ticket.key, request())
      await qc.invalidateQueries()
      toast.success(`${d.title}: signed with Touch ID`)
      onClose()
    } catch (e) {
      setPhase('confirm')
      setError(e instanceof ApiError ? e.message : 'Could not sign')
    }
  }

  const openEvidence = () => {
    onClose()
    if (onOpenEvidence) onOpenEvidence()
    else void navigate({ to: '/ticket/$key', params: { key: ticket.key } })
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent
        className="max-w-lg gap-4 border-border bg-surface"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(action.kind === 'verdict' ? firstRadio.current : cancel.current)?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-brand" />
            {d.title}
          </DialogTitle>
          <DialogDescription>
            Signed with your key on {ticket.key}. Only core shows this prompt; an addon cannot sign for you.
            {d.policy && <span className="block text-[12px] text-text-faint">{d.policy}</span>}
          </DialogDescription>
        </DialogHeader>

        {sections && hasContent && <Signed sections={sections} />}
        {nothing && (
          <p role="status" className="rounded-md border border-dashed border-border px-3 py-2 text-[13px] text-text-muted">
            {hint}
          </p>
        )}

        {action.kind === 'answer' && (
          <div className="space-y-1 rounded-md border border-border bg-bg p-3 text-[13px]">
            {d.covers.slice(0, 2).map((c) => (
              <p key={c} className="break-words text-text">
                {c}
              </p>
            ))}
          </div>
        )}

        {action.kind === 'verdict' && (
          <>
            <p className="flex flex-wrap items-center gap-x-3 text-[13px] text-text-muted">
              <span>
                AC {proven}/{ticket.acceptance_state.length} evidenced · {receipts} {receipts === 1 ? 'receipt' : 'receipts'}
              </span>
              <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[13px]" onClick={openEvidence}>
                Open evidence
              </Button>
            </p>
            <fieldset className="grid gap-2" disabled={busy}>
              <legend className="sr-only">Verdict</legend>
              {(
                [
                  ['pass', 'Pass · the evidence is enough'],
                  ['fail', 'Send back · something must change'],
                ] as const
              ).map(([r, label], i) => (
                <label
                  key={r}
                  className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-[13px] has-[:checked]:border-brand has-[:checked]:bg-brand-soft"
                >
                  <input ref={i === 0 ? firstRadio : undefined} type="radio" name="verdict" value={r} checked={result === r} onChange={() => setResult(r)} className="accent-[var(--brand)]" />
                  {label}
                </label>
              ))}
            </fieldset>
          </>
        )}

        {(needsText || action.kind === 'verdict') && (
          <div className="space-y-1.5">
            <Label htmlFor="sign-text">{needsText ? 'What should change? (required)' : 'Note (optional)'}</Label>
            <Textarea id="sign-text" value={text} onChange={(e) => setText(e.target.value)} disabled={busy} rows={3} />
          </div>
        )}

        <SignDetails hash={d.hash} covers={action.kind === 'answer' ? d.covers.slice(2) : d.covers} />

        {error && (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-[13px] text-danger">
            {error}
          </p>
        )}

        <DialogFooter className="items-center gap-2 sm:justify-between">
          <span aria-live="polite" className="flex items-center gap-1.5 text-[12px] text-text-muted">
            {phase === 'touch' && (
              <>
                <Fingerprint className="size-4 animate-pulse text-brand" />
                Touch the sensor to confirm
              </>
            )}
            {phase === 'sending' && (
              <>
                <Loader2 className="size-4 animate-spin" />
                Signing
              </>
            )}
            {phase === 'confirm' && hint && !nothing && <span id="sign-hint">{hint}</span>}
          </span>
          <div className="flex gap-2">
            <Button ref={cancel} variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={run} disabled={blocked} aria-describedby={hint && !nothing ? 'sign-hint' : undefined}>
              {verb}
            </Button>
          </div>
        </DialogFooter>
        <ConfirmHelper />
      </DialogContent>
    </Dialog>
  )
}
