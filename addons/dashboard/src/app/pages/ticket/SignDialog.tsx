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
import { commitCover, diffstat, gateSignedContent, sectionWritten, type SignedField, type SignedSection } from '@/api/gates'
import { Inline, Prose, Raw, visible } from '@/components/sign/visible'
import { GATE_LABEL, policyText } from './actions'
import type { HumanAction } from './shared'
import { queries } from '@/api/queries'

type Described = { title: string; gate?: GateName; hash: string; covers: string[]; policy?: string }

/** The dialog title and what the signature covers, in core's words (the answer itself is shown field by field, see Answer). */
function describe(ticket: TicketDocument, a: HumanAction): Described {
  if (a.kind === 'answer') {
    const q = ticket.questions_state.find((x) => x.id === a.question)!
    return { title: `Answer ${q.id}`, hash: q.hash ?? '', covers: ['The question, its options and its current hash', 'Your answer: the option and the text shown above'] }
  }
  const gate: GateName = a.kind === 'verdict' ? 'verify' : a.gate
  const g = ticket.gates[gate]
  const name = GATE_LABEL[gate].toLowerCase()
  const title = a.kind === 'approve' ? `Approve ${name}` : a.kind === 'verdict' ? 'Give a verdict' : `Request changes on ${name}`
  return { title, gate, hash: g.hash ?? '', covers: g.covers ?? [], policy: policyText(gate, g) }
}

/** What an approval signs, from the same fields the gate hash covers (api/gates.ts). Type and size are shown but never count as content. */
function signedSections(ticket: TicketDocument, gate: GateName, personName: (id: string) => string): SignedSection[] | null {
  if (gate === 'verify') return null
  // The code review signs exactly the branch head and its diff against the base (the same commit the verdict signed).
  if (gate === 'code')
    return [
      { label: 'Commit', text: commitCover(ticket.branch) },
      { label: 'Commits on the branch', text: ticket.branch.commits.map((c) => `${c.sha}  ${c.task ? `${c.task} · ` : ''}${personName(c.by)}`).join('\n') },
    ]
  return gateSignedContent(gate, ticket, personName).sections
}

const written = (sections: SignedSection[]) => sections.filter(sectionWritten)

/** One field of a signed item: the ticket's own text on its own line (escaped, newlines shown), core's fields labelled. */
function Field({ f }: { f: SignedField }) {
  const value = Array.isArray(f.value)
    ? f.value.map((v, i) => (
        <span key={i}>
          {i > 0 && ', '}
          <Raw>{v}</Raw>
        </span>
      ))
    : f.mono
      ? <Raw>{f.value}</Raw>
      : <Inline className="text-text">{f.value}</Inline>
  return (
    <p data-signed-field={f.name} data-source={f.source} className={f.label ? 'pl-4 text-[12px] text-text-muted' : 'text-text'}>
      {f.label && `${f.label}: `}
      {value}
    </p>
  )
}

/**
 * What an approval signs, each section and field on its own (security review #4): ticket text is shown exactly as
 * hashed through the visible-string helpers (bidi-isolated, invisible characters as escapes); a list item's text cannot
 * spill into core's labelled fields below it. The caption says who wrote it.
 */
function Signed({ sections }: { sections: SignedSection[] }) {
  return (
    <div className="max-h-[40vh] space-y-3 overflow-auto rounded-md border border-border bg-bg p-3 text-[13px]">
      <p className="text-[11px] text-text-faint">Text from the ticket, exactly as signed (written by its people and agents).</p>
      {sections.filter((s) => s.meta || sectionWritten(s)).map((s) => (
        <section key={s.label} aria-label={s.label}>
          <h3 className="mb-1 text-[12px] font-medium text-text-muted">{s.label}</h3>
          {s.items ? (
            <ul className="space-y-1.5">
              {s.items.map((it) => (
                <li key={it.id} data-signed-item={it.id}>
                  {it.fields.map((f, i) =>
                    i === 0 && f.name === 'text' ? (
                      <p key={f.name} data-signed-field="text" data-source={f.source} className="text-text">
                        <Raw>{it.id}</Raw> <Inline>{f.value as string}</Inline>
                      </p>
                    ) : (
                      <Field key={f.name} f={f} />
                    ),
                  )}
                </li>
              ))}
            </ul>
          ) : s.meta ? (
            <p className="text-text">{s.text}</p>
          ) : (
            <Prose className="break-words text-text">{s.text}</Prose>
          )}
        </section>
      ))}
    </div>
  )
}

/** The answer, in core's area, field by field and exactly as it is posted (security review #7). */
function Answer({ ticket, question, option, text }: { ticket: TicketDocument; question: string; option?: string; text?: string }) {
  const q = ticket.questions_state.find((x) => x.id === question)!
  const picked = q.options?.find((o) => o.key === option)
  return (
    <div className="max-h-[40vh] space-y-2 overflow-auto rounded-md border border-border bg-bg p-3 text-[13px]">
      <section aria-label="Question">
        <p className="text-[12px] font-medium text-text-muted">
          Question <Raw>{q.id}</Raw>:
        </p>
        <Prose className="text-text">{q.text}</Prose>
      </section>
      <section aria-label="Your answer">
        {picked ? (
          <p data-signed-field="option" className="text-text">
            Your answer: <Inline>{picked.label}</Inline> (option <Raw>{picked.key}</Raw>)
          </p>
        ) : (
          <p className="text-text">Your answer: free text</p>
        )}
        {text && (
          <div data-signed-field="text">
            <p className="text-[12px] text-text-muted">{picked ? 'Your note, sent with it:' : 'Your text:'}</p>
            <Prose className="text-text">{text}</Prose>
          </div>
        )}
      </section>
    </div>
  )
}

/**
 * Core's signing dialog for a ticket action. It shows what is signed (the text and the list), names the verb on the
 * button, keeps the hash in a closed Details, and starts with focus on Cancel (the first radio for a verdict).
 * `onOpenEvidence` lets the ticket page jump to the evidence; without it the dialog closes and opens the ticket.
 */
export function SignDialog({ ticket, action, onClose, onOpenEvidence, onOpenChanges, onPending }: { onPending?: (pending: boolean) => void; ticket: TicketDocument; action: HumanAction | null; onClose: () => void; onOpenEvidence?: () => void; onOpenChanges?: () => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [phase, setPhase] = useState<'confirm' | 'touch' | 'sending'>('confirm')
  const [text, setText] = useState('')
  const [result, setResult] = useState<'pass' | 'fail' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const workspaces = useQuery(queries.workspaces())
  const members = workspaceOfTicket(ticket.key, workspaces.data ?? [])?.members ?? []
  const personName = (id: string) => (workspaces.data ? (members.find((m) => m.person === id)?.name ?? id) : '…') // not the raw id while the workspaces load
  const cancel = useRef<HTMLButtonElement>(null)
  const firstRadio = useRef<HTMLInputElement>(null)

  // What is signed is the ticket as it was when the dialog opened (security review #2): a refetch while it is open (an
  // agent's edit, a push) does not change what is shown or posted; the host refuses the stale hash or sha (409), and
  // the dialog says that the ticket changed.
  const [snap, setSnap] = useState<{ action: HumanAction | null; ticket: TicketDocument }>({ action, ticket })
  if (snap.action !== action) setSnap({ action, ticket })
  const opened = snap.action === action ? snap.ticket : ticket
  useEffect(() => {
    setPhase('confirm')
    setText('')
    setResult(null)
    setError(null)
  }, [action])

  if (!action) return null
  const d = describe(opened, action)
  const live = describe(ticket, action)
  // Content-bound signatures (an answer, requirements, plan) say when the content moved; a verdict or code review keeps
  // its existing behaviour (the commit shown is signed, a newer one is refused with verdict.stale / gate.stale).
  const changed = (action.kind === 'answer' || d.gate === 'requirements' || d.gate === 'plan') && live.hash !== d.hash
  const sections = action.kind === 'approve' || action.kind === 'request_changes' ? (d.gate ? signedSections(opened, d.gate, personName) : null) : null
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

  // The commit a verdict or code review signs: the branch head when the dialog opened, in full (owner decision 2026-10-10).
  const head = opened.branch.head
  const shownBranch = opened.branch
  const passLabel = `Pass on ${visible(head)} · ${diffstat(opened.branch)}: the evidence is enough`
  const proven = ticket.acceptance_state.filter((a) => a.state === 'proven').length
  const receipts = ticket.tasks_state.filter((t) => t.receipt).length

  const answerText = action.kind === 'answer' ? action.text?.trim() || undefined : undefined
  const request = (): ActionRequest => {
    switch (action.kind) {
      case 'approve':
        // Requirements and plan bind the content hash shown (security review #2); the code review binds the commit.
        return action.gate === 'code' ? { action: 'approve', gate: 'code', source_sha: head } : { action: 'approve', gate: action.gate, hash: d.hash }
      case 'request_changes':
        return { action: 'request_changes', gate: action.gate, text }
      case 'verdict':
        return { action: 'verdict', result: result!, text: text || undefined, source_sha: head }
      case 'answer':
        // Exactly what the dialog shows: the option, the trimmed text, and the question's hash when it opened.
        return { action: 'answer', question: action.question, ...(action.option ? { option: action.option } : {}), ...(answerText ? { text: answerText } : {}), hash: d.hash }
    }
  }

  const run = async () => {
    if (blocked) return
    setError(null)
    setPhase('touch')
    onPending?.(true)
    await new Promise((r) => setTimeout(r, TOUCH_ID_MS))
    setPhase('sending')
    try {
      await api.postAction(ticket.key, request())
      await qc.invalidateQueries()
      toast.success(`${d.title}: signed with Touch ID`)
      onPending?.(false)
      onClose()
    } catch (e) {
      setPhase('confirm')
      onPending?.(false)
      setError(e instanceof ApiError ? e.message : 'Could not sign')
    }
  }

  const openEvidence = () => {
    onClose()
    if (onOpenEvidence) onOpenEvidence()
    else void navigate({ to: '/ticket/$key', params: { key: ticket.key } })
  }
  const openChanges = () => {
    onClose()
    if (onOpenChanges) onOpenChanges()
    else void navigate({ to: '/ticket/$key', params: { key: ticket.key } })
  }
  const changesLink = (
    <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[13px]" onClick={openChanges} disabled={busy}>
      Open changes
    </Button>
  )

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
        {d.gate === 'code' && <p className="text-[13px] text-text-muted">Read the diff before you approve: {changesLink}</p>}
        {nothing && (
          <p role="status" className="rounded-md border border-dashed border-border px-3 py-2 text-[13px] text-text-muted">
            {hint}
          </p>
        )}

        {action.kind === 'answer' && <Answer ticket={opened} question={action.question} option={action.option} text={answerText} />}
        {changed && (
          <p role="alert" className="text-[13px] text-warning">
            This ticket changed after you opened this dialog. You sign what is shown here; orch refuses it if it no longer matches. Close and reopen it to see the current version.
          </p>
        )}

        {action.kind === 'verdict' && (
          <>
            <p className="flex flex-wrap items-center gap-x-3 text-[13px] text-text-muted">
              <span>
                AC {proven}/{ticket.acceptance_state.length} evidenced · {receipts} {receipts === 1 ? 'receipt' : 'receipts'}
              </span>
              <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[13px]" onClick={openEvidence} disabled={busy}>
                Open evidence
              </Button>
              {changesLink}
            </p>
            <section aria-label="Commit" className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
              <h3 className="mb-0.5 text-[12px] font-medium text-text-muted">The verdict signs this commit</h3>
              <p className="break-words font-mono text-[12px] text-text">{visible(commitCover(shownBranch))}</p>
              <p className="mt-1 text-[12px] text-text-muted">A new commit on the branch after the verdict voids it; the ticket goes back to testing.</p>
            </section>
            <fieldset className="grid gap-2" disabled={busy}>
              <legend className="sr-only">Verdict</legend>
              {(
                [
                  ['pass', passLabel],
                  ['fail', 'Send back · something must change'],
                ] as const
              ).map(([r, label], i) => (
                <label
                  key={r}
                  className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-[13px] has-[:checked]:border-brand has-[:checked]:bg-brand-soft"
                >
                  <input ref={i === 0 ? firstRadio : undefined} type="radio" id={`verdict-${r}`} name="verdict" value={r} checked={result === r} onChange={() => setResult(r)} className="accent-[var(--brand)]" />
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

        <SignDetails hash={d.hash} covers={d.covers} />

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
                Signing…
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
