import { useQueryClient } from '@tanstack/react-query'
import { Fingerprint, Loader2, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, type ActionRequest, type GateName, type TicketDocument } from '@/api/types'
import { TOUCH_ID_MS } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { GATE_LABEL, policyText } from './actions'
import { Mono, type HumanAction } from './shared'

/** What the signature covers, shown before anything is signed. */
function describe(ticket: TicketDocument, a: HumanAction): { title: string; gate?: GateName; hash: string; covers: string[]; policy?: string; verb: string } {
  if (a.kind === 'answer') {
    const q = ticket.questions_state.find((x) => x.id === a.question)!
    const picked = q.options?.find((o) => o.key === a.option)
    return {
      title: `Answer ${q.id}`,
      hash: q.hash ?? '',
      covers: [`Question ${q.id}: ${q.text}`, picked ? `Your answer: ${picked.label}` : 'Your answer: free text', 'The current hash of the question'],
      verb: 'Answer',
    }
  }
  const gate: GateName = a.kind === 'verdict' ? 'verify' : a.gate
  const g = ticket.gates[gate]
  const title =
    a.kind === 'approve' ? `Approve ${GATE_LABEL[gate].toLowerCase()}` : a.kind === 'verdict' ? 'Give a verdict' : `Request changes on ${GATE_LABEL[gate].toLowerCase()}`
  return { title, gate, hash: g.hash ?? '', covers: g.covers ?? [], policy: policyText(g), verb: a.kind === 'approve' ? 'Approve' : a.kind === 'verdict' ? 'Verdict' : 'Request changes' }
}

export function SignDialog({ ticket, action, onClose }: { ticket: TicketDocument; action: HumanAction | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [phase, setPhase] = useState<'confirm' | 'touch' | 'sending'>('confirm')
  const [text, setText] = useState('')
  const [result, setResult] = useState<'pass' | 'fail'>('pass')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPhase('confirm')
    setText('')
    setResult('pass')
    setError(null)
  }, [action])

  if (!action) return null
  const d = describe(ticket, action)
  const needsText = action.kind === 'request_changes' || (action.kind === 'verdict' && result === 'fail')
  const busy = phase !== 'confirm'

  const request = (): ActionRequest => {
    switch (action.kind) {
      case 'approve':
        return { action: 'approve', gate: action.gate }
      case 'request_changes':
        return { action: 'request_changes', gate: action.gate, text }
      case 'verdict':
        return { action: 'verdict', result, text: text || undefined }
      case 'answer':
        return { action: 'answer', question: action.question, option: action.option, text: action.text }
    }
  }

  const run = async () => {
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

  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent className="max-w-lg gap-4 border-border bg-surface">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-brand" />
            {d.title}
          </DialogTitle>
          <DialogDescription>
            This is signed with your key on {ticket.key}. Only core shows this prompt; an addon cannot sign for you.
          </DialogDescription>
        </DialogHeader>

        <dl className="grid grid-cols-[88px_1fr] gap-x-3 gap-y-2 rounded-md border border-border bg-bg p-3 text-[13px]">
          {d.gate && (
            <>
              <dt className="text-text-muted">Gate</dt>
              <dd>
                {GATE_LABEL[d.gate]} <span className="text-text-faint">({d.policy})</span>
              </dd>
            </>
          )}
          <dt className="text-text-muted">Hash</dt>
          <dd>
            <Mono className="break-all text-text">{d.hash}</Mono>
          </dd>
          <dt className="text-text-muted">Covers</dt>
          <dd>
            <ul className="list-disc space-y-0.5 pl-4">
              {d.covers.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </dd>
        </dl>

        {action.kind === 'verdict' && (
          <fieldset className="flex gap-2" disabled={busy}>
            <legend className="sr-only">Verdict</legend>
            {(['pass', 'fail'] as const).map((r) => (
              <label
                key={r}
                className="flex flex-1 cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-[13px] has-[:checked]:border-brand has-[:checked]:bg-brand-soft"
              >
                <input type="radio" name="verdict" value={r} checked={result === r} onChange={() => setResult(r)} className="accent-[var(--brand)]" />
                {r === 'pass' ? 'Pass: evidence is enough' : 'Fail: send back'}
              </label>
            ))}
          </fieldset>
        )}

        {(needsText || action.kind === 'verdict') && (
          <div className="space-y-1.5">
            <Label htmlFor="sign-text">{needsText ? 'What should change?' : 'Note (optional)'}</Label>
            <Textarea id="sign-text" value={text} onChange={(e) => setText(e.target.value)} disabled={busy} rows={3} />
          </div>
        )}

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
          </span>
          <div className="flex gap-2">
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={run} disabled={busy || (needsText && !text.trim())}>
              <Fingerprint />
              Sign with Touch ID
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
