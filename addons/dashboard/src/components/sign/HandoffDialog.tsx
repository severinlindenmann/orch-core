// Core's signing dialog in hand-off mode. Same place, same core words in the title and the buttons as the simulated
// Touch ID dialog it replaces, but the dashboard does not sign: it prepares a request with the host, shows the host's
// material, the fingerprint, the code and the exact terminal command, and follows the outcome (design 4).
import { Loader2, ShieldCheck } from 'lucide-react'
import { useEffect, useRef, type ReactNode } from 'react'
import { toast } from 'sonner'
import type { HandoffAction, HandoffRequest, HandoffState } from '@/api/handoff'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { HandoffPanel } from './HandoffPanel'
import { failedSubmit, useHandoff, type HandoffPhase } from './useHandoff'

interface Words {
  title: string
  body: string
  tone?: 'ok' | 'warn'
}

/** What each end state says, in core's words. */
function words(phase: HandoffPhase, title: string, error: string | null, ofWorkspace = false): Words | null {
  switch (phase) {
    case 'done':
      return { title: 'Signed', body: `${title} is in the ${ofWorkspace ? "workspace's" : "ticket's"} log.`, tone: 'ok' }
    case 'cancelled':
      return { title: 'Cancelled', body: 'Nothing was signed.' }
    case 'expired':
      return { title: 'Expired', body: 'The request ran out of time before it was signed. Prepare it again to get a new code.', tone: 'warn' }
    case 'already':
      return { title: 'Already done', body: 'The host says this is already in place, so there is nothing to sign. The page shows the current state.' }
    case 'changed':
      return { title: 'Changed since you started', body: 'The ticket changed. The page now shows the current state. Prepare again to sign what is there now.', tone: 'warn' }
    case 'unknown':
      return { title: 'Unknown outcome', body: 'The dashboard lost track of this request. An earlier attempt may still be open; it expires by itself within 10 minutes. The page now shows the current state. Prepare again only if the change is not there; that is a new request, and the earlier one is only cancelled when it is provably from this dashboard session.', tone: 'warn' }
    case 'invalid':
      return { title: 'Not shown', body: 'The host sent a request whose fingerprint does not match its content, so it is not shown and was cancelled. Nothing was signed.', tone: 'warn' }
    case 'error':
      return { title: 'Could not prepare', body: error ?? 'The request could not be prepared.', tone: 'warn' }
    default:
      return null
  }
}

export interface HandoffDialogProps {
  /** Core's title: "Approve plan", "Comment", "Close DEMO-0043". */
  title: string
  ws: string | undefined
  /** What to prepare. Null while the form is not complete (the button stays off). */
  action: HandoffAction | null
  /** Prepare as soon as the dialog opens (an action with nothing to fill in). The click that opened it is the intent. */
  autoStart?: boolean
  /** Button that starts the preparation when there is a form: the verb, in core's words. */
  confirmLabel?: string
  /** Why the button is off, shown beside it. */
  hint?: string | null
  /** The form, before the request exists. */
  children?: ReactNode
  /** Said above the panel while it is open: a warning that the page changed under it. */
  warning?: (request: HandoffRequest) => string | null
  /** The signer's event is in the log. */
  onSettled?: (phase: HandoffPhase, state: HandoffState | null, request: HandoffRequest | null) => void
  onPending?: (pending: boolean) => void
  onClose: () => void
  initialFocus?: () => HTMLElement | null
}

export function HandoffDialog(p: HandoffDialogProps) {
  const run = useHandoff(p.ws, (phase, state, request) => {
    if (phase === 'done') toast.success(`${p.title}: signed in your terminal`)
    p.onSettled?.(phase, state, request)
  })
  const { phase } = run
  const started = useRef(false)
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (p.autoStart && p.action && !started.current && p.ws) {
      started.current = true
      void run.prepare(p.action)
    }
    // Once per opening: a changed action object is not a new click.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.autoStart, p.ws])

  // Switching workspace cancels the open request at the host; the dialog belongs to the old workspace and closes.
  useEffect(() => {
    if (!run.switched) return
    toast(`${p.title}: cancelled, you switched workspace. Nothing was signed.`)
    p.onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.switched])

  const open = phase === 'preparing' || phase === 'waiting'
  useEffect(() => {
    p.onPending?.(open)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const close = async () => {
    if (phase === 'waiting' || phase === 'invalid') await run.cancel()
    p.onClose()
  }
  const again = () => {
    if (!p.action) return
    run.reset()
    void run.prepare(p.action)
  }
  const end = words(phase, p.title, run.error?.message ?? null, !!run.request && !run.request.material.ticket)

  return (
    <Dialog open onOpenChange={(o) => !o && phase !== 'preparing' && void close()}>
      <DialogContent
        data-handoff-dialog
        className="max-w-lg gap-4 border-border bg-surface"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(p.initialFocus?.() ?? cancelRef.current)?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-brand" />
            {p.title}
          </DialogTitle>
          <DialogDescription>
            {phase === 'idle' ? 'You sign in your terminal. This dashboard prepares the request and never signs.' : 'Signed with your key in your terminal. Only core prepares this request; an agent or an addon cannot sign it.'}
          </DialogDescription>
        </DialogHeader>

        {phase === 'idle' && p.children}

        {phase === 'preparing' && (
          <p role="status" className="flex items-center gap-2 text-[13px] text-text-muted">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            Preparing the request…
          </p>
        )}

        {phase === 'waiting' && run.request && (
          <>
            {p.warning?.(run.request) && (
              <p role="alert" className="rounded-md border border-warning/40 px-3 py-2 text-[13px] text-warning">
                {p.warning(run.request)}
              </p>
            )}
            {run.state?.error?.code && (
              <p role="alert" className="rounded-md border border-warning/40 px-3 py-2 text-[13px] text-warning">
                Refused: {failedSubmit(run.state.error.code)} The request stays open until {new Date(run.request.expires_at).toLocaleTimeString()}.
              </p>
            )}
            {run.request.earlier_open && (
              <p role="status" data-testid="earlier-open" className="rounded-md border border-warning/40 px-3 py-2 text-[13px] text-warning">
                An earlier attempt may still be open; it expires by itself {run.request.earlier_open.expires_at ? `at ${new Date(run.request.earlier_open.expires_at).toLocaleTimeString()}` : 'within 10 minutes'}. Sign only the code shown here.
              </p>
            )}
            <HandoffPanel request={run.request} />
          </>
        )}

        {end && (
          <div role={end.tone === 'warn' ? 'alert' : 'status'} data-handoff-end={phase} className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
            <p className="font-medium text-text">{end.title}</p>
            <p className="text-text-muted">{end.body}</p>
            {run.error?.hint && phase === 'error' && <p className="text-text-faint">{run.error.hint}</p>}
          </div>
        )}

        <DialogFooter className="items-center gap-2 sm:justify-between">
          <span aria-live="polite" className="text-[12px] text-text-muted">
            {phase === 'idle' && p.hint}
            {phase === 'waiting' && 'Waiting for your signature'}
          </span>
          <div className="flex flex-wrap gap-2">
            {phase === 'idle' && (
              <>
                <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
                  Cancel
                </Button>
                <Button onClick={() => p.action && void run.prepare(p.action)} disabled={!p.action || !p.ws || !!p.hint}>
                  {p.confirmLabel ?? p.title}
                </Button>
              </>
            )}
            {phase === 'preparing' && (
              <Button ref={cancelRef} variant="ghost" disabled>
                Cancel
              </Button>
            )}
            {phase === 'waiting' && (
              <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
                Cancel
              </Button>
            )}
            {(phase === 'expired' || phase === 'changed' || phase === 'unknown' || phase === 'error') && p.action && (
              <Button onClick={again}>{phase === 'error' ? 'Try again' : 'Prepare again'}</Button>
            )}
            {end && (
              <Button ref={cancelRef} variant={phase === 'done' ? 'default' : 'ghost'} onClick={() => void close()}>
                Close
              </Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
