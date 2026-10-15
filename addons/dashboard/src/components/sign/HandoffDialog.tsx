// Core's signing dialog in hand-off mode. Same place, same core words in the title and the buttons as the simulated
// Touch ID dialog it replaces, but the dashboard does not sign: it prepares a request with the host, shows the host's
// material, the fingerprint, the code and the exact terminal command, and follows the outcome (design 4).
import { Loader2, ShieldCheck } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import type { HandoffAction, HandoffRequest, HandoffState } from '@/api/handoff'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { queries } from '@/api/queries'
import { sharesPrefix, uuid8 } from '@/api/workspaces'
import { useWorkspace } from '@/app/workspace'
import { alreadySigned, clickEligible, clickReady } from '@/api/click'
import { ClickRisk, ClickSignPanel } from './ClickSignPanel'
import { useClickSign } from './useClickSign'
import { HandoffPanel } from './HandoffPanel'
import { Raw } from './visible'
import { failedSubmit, useHandoff, type HandoffPhase } from './useHandoff'

interface Words {
  title: string
  body: string
  tone?: 'ok' | 'warn'
}

/** What each end state says, in core's words. */
function words(phase: HandoffPhase, title: string, error: string | null, ofWorkspace = false, code?: string): Words | null {
  switch (phase) {
    case 'done':
      return { title: 'Signed', body: `${title} is in the ${ofWorkspace ? "workspace's" : "ticket's"} log.`, tone: 'ok' }
    case 'cancelled':
      return { title: 'Cancelled', body: 'Nothing was signed.' }
    case 'expired':
      if (code === 'intent.expired') return { title: 'This attempt is too old; start again', body: error ?? '', tone: 'warn' }
      return { title: 'Expired', body: 'The request ran out of time before it was signed. Prepare it again to get a new code.', tone: 'warn' }
    case 'already':
      return { title: 'Already done', body: code === 'intent.done' ? error ?? '' : 'The host says this is already in place, so there is nothing to sign. The page shows the current state. If you meant to do it again, say so.' }
    case 'changed':
      return { title: 'Changed since you started', body: 'The ticket changed. The page now shows the current state. Prepare again to sign what is there now.', tone: 'warn' }
    case 'unknown':
      return { title: 'Unknown outcome', body: 'The dashboard lost track of this request (an answer was lost). Try again asks the host for the same request with the same key: it returns the request it already made, in its current state, and never a second one. The page shows the current state.', tone: 'warn' }
    case 'failed':
      return { title: 'The earlier attempt did not land', body: error ?? '', tone: 'warn' }
    case 'blocked':
      return { title: 'The earlier attempt ended', body: error ?? '', tone: 'warn' }
    case 'attempt_open':
      return { title: 'An attempt for this is already open', body: error ?? 'The host holds an open request for this action. Wait until it ends or expires.', tone: 'warn' }
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
  /** Exact action summary, kept visible while preparing and reviewing. */
  summary?: ReactNode
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
  const { workspaces } = useWorkspace()
  // The workspace's card (the signer's flag) comes from the provider, or from the list when none wraps this dialog.
  const known = useQuery(queries.workspaces())
  const here = workspaces.find((w) => w.id === p.ws) ?? known.data?.find((w) => w.id === p.ws)
  // Click-to-sign (H5c): offered where the signer says the flag is on and it can sign, for the actions it serves.
  const click = useClickSign(p.ws, (_phase, state, request) => {
    toast.success(`${p.title}: signed`)
    p.onSettled?.('done', state, request)
  })
  const canClick = clickReady(here) && clickEligible(p.action)
  // ONE signing path per workspace (owner decision, 12 Oct 2026): where click-to-sign is on, there is no terminal hand-off.
  const clickOnly = !!here?.click?.enabled
  const clickOpen = click.phase !== 'idle'
  const [waitUntil, setWaitUntil] = useState(0)
  const shared = !!here && workspaces.some((o) => o.id !== here.id && sharesPrefix(o, here))
  const started = useRef(false)
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    // Not before the list of workspaces is known: whether this one offers Sign decides how the dialog starts.
    if (p.autoStart && p.action && !started.current && p.ws && !known.isPending && !canClick && !clickOnly) {
      started.current = true
      void run.prepare(p.action)
    }
    // Once per opening: a changed action object is not a new click.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.autoStart, p.ws, known.isPending, canClick])

  // Switching workspace cancels the open request at the host; the dialog belongs to the old workspace and closes.
  useEffect(() => {
    if (!run.switched) return
    toast(`${p.title}: cancelled, you switched workspace. Nothing was signed.`)
    p.onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.switched])

  // The click request lives in this dialog too: switching workspace cancels it like the hand-off.
  useEffect(() => {
    if (!click.switched) return
    toast(`${p.title}: cancelled, you switched workspace. Nothing was signed.`)
    p.onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [click.switched])

  useEffect(() => {
    if (!click.leftUnsettled) return
    toast(`${p.title}: you switched workspace while its signature is not settled. Check the ticket before signing it again.`)
    p.onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [click.leftUnsettled])

  const clickLive = ['preparing', 'viewing', 'ready', 'signing', 'settling'].includes(click.phase)
  const open = phase === 'preparing' || phase === 'waiting' || clickLive
  useEffect(() => {
    p.onPending?.(open)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (click.problem?.waitMs) setWaitUntil(Date.now() + click.problem.waitMs)
  }, [click.problem])

  const close = async () => {
    if (phase === 'waiting' || phase === 'invalid') await run.cancel()
    // Only Cancel cancels. When the outcome is unknown, closing must not cancel: the signature may be on its way.
    if (click.prepared && click.problem?.next !== 'check' && !['done', 'cancelled', 'idle'].includes(click.phase)) await click.cancel()
    p.onClose()
  }
  const prepareClickAgain = () => {
    if (p.action) void click.again(p.action) // cancels an open request first: no leaked slot
  }
  const again = () => {
    if (!p.action) return
    run.reset(phase === 'unknown' || phase === 'error') // the same key where the host may already have the request
    void run.prepare(p.action)
  }
  const end = words(phase, p.title, run.error?.code === 'intent.done' ? alreadySigned(run.error) : run.error?.message ?? null, !!run.request && !run.request.material.ticket, run.error?.code)

  return (
    <Dialog open onOpenChange={(o) => !o && phase !== 'preparing' && click.phase !== 'preparing' && click.phase !== 'signing' && void close()}>
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
            <bdi>{p.title}</bdi>
          </DialogTitle>
          {shared && here && (
            <p data-testid="handoff-workspace" className="text-[12px] text-text-muted">
              Workspace {here.prefix}{' '}
              <span className="rounded bg-surface-3 px-1 font-mono text-text" data-ws-id>
                id {uuid8(here.id)}
              </span>{' '}
              · <bdi>
                <Raw>{here.name}</Raw>
              </bdi>
            </p>
          )}
          <DialogDescription>
            {clickOpen
              ? 'Signed with your key by core’s signing helper, only after you press Sign. An agent or an addon cannot prepare this request.'
              : phase === 'idle'
                ? canClick
                  ? 'Sign here with one click. Core prepares the request and its signing helper signs it.'
                  : clickOnly
                    ? 'This workspace signs in the dashboard.'
                    : 'You sign in your terminal. This dashboard prepares the request and never signs.'
                : 'Signed with your key in your terminal. Only core prepares this request; an agent or an addon cannot sign it.'}
          </DialogDescription>
        </DialogHeader>

        {p.summary}
        {phase === 'idle' && !clickOpen && p.children}
        {phase === 'idle' && !clickOpen && canClick && <ClickRisk />}
        {phase === 'idle' && !clickOpen && clickOnly && !canClick && (
          <p role="alert" data-testid="click-unavailable" className="rounded-md border border-warning/40 px-3 py-2 text-[13px] text-warning">
            The signing helper cannot sign right now{here?.click?.problem ? ` (${here.click.problem})` : ''}. Fix that, or turn convenience off from the terminal with <code>orch host convenience off --workspace {here?.prefix ?? 'W'}</code>. Nothing was prepared.
          </p>
        )}

        {(click.phase === 'preparing' || click.phase === 'viewing') && (
          <p role="status" className="flex items-center gap-2 text-[13px] text-text-muted">
            <Loader2 className="size-4 animate-spin" aria-hidden />
            {click.phase === 'preparing' ? 'Preparing the request…' : 'Reading the review…'}
          </p>
        )}

        {(click.phase === 'ready' || click.phase === 'signing') && click.view && (
          <ClickSignPanel
            answer={click.view}
            problem={click.problem}
            signing={click.phase === 'signing'}
            onSign={(word) => void click.sign(word)}
            onCancel={() => void close()}
            waitUntil={waitUntil}
          />
        )}

        {click.phase === 'settling' && (
          <div role="status" data-testid="click-settling" className="flex items-start gap-2 rounded-md border border-warning/40 px-3 py-2 text-[13px]">
            <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" aria-hidden />
            <div>
              <p className="font-medium text-text">The outcome is not certain yet</p>
              <p className="text-text-muted">The signature may already be at the host. It settles by itself; this updates when it does. Do not sign again.</p>
              {click.settleNote && <p data-testid="click-settle-note" className="mt-1 text-text">{click.settleNote}</p>}
            </div>
          </div>
        )}

        {click.phase === 'failed' && click.problem && (
          <div role="alert" data-testid="click-problem" data-click-next={click.problem.next} className="rounded-md border border-warning/40 px-3 py-2 text-[13px]">
            <p className="font-medium text-text">{click.problem.title}</p>
            <p title={click.problem.signedAt ?? undefined} className="text-text-muted"><Raw>{click.problem.body}</Raw></p>
          </div>
        )}

        {click.phase === 'done' && (
          <div role="status" data-click-end="done" className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
            <p className="font-medium text-text">Signed</p>
            <p className="text-text-muted">{p.title} is in the log.</p>
          </div>
        )}
        {click.phase === 'cancelled' && (
          <div role="status" data-click-end="cancelled" className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
            <p className="font-medium text-text">Cancelled</p>
            <p className="text-text-muted">Nothing was signed.</p>
          </div>
        )}

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
            <HandoffPanel request={run.request} workspace={here ?? null} />
          </>
        )}

        {end && (
          <div role={end.tone === 'warn' ? 'alert' : 'status'} data-handoff-end={phase} className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
            <p className="font-medium text-text">{end.title}</p>
            <p title={run.error?.signed_at ?? undefined} className="text-text-muted"><Raw>{end.body}</Raw></p>
            {run.error?.fix && <p><Raw>{typeof run.error.fix === 'string' ? run.error.fix : JSON.stringify(run.error.fix)}</Raw></p>}
            {run.error?.hint && phase === 'error' && <p className="text-text-faint"><Raw>{run.error.hint}</Raw></p>}
          </div>
        )}

        <DialogFooter className="items-center gap-2 sm:justify-between">
          <span aria-live="polite" className="text-[12px] text-text-muted">
            {phase === 'idle' && p.hint}
            {phase === 'waiting' && 'Waiting for your signature'}
          </span>
          <div className="flex flex-wrap gap-2">
            {phase === 'idle' && !clickOpen && (
              <>
                <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
                  Cancel
                </Button>
                {!clickOnly || canClick ? (
                  <Button
                    onClick={() => (canClick ? p.action && void click.start(p.action) : p.action && void run.prepare(p.action))}
                    disabled={!p.action || !p.ws || !!p.hint}
                  >
                    {canClick ? 'Sign' : (p.confirmLabel ?? p.title)}
                  </Button>
                ) : null}
              </>
            )}
            {click.phase === 'settling' && (
              <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
                Close
              </Button>
            )}
            {click.phase === 'failed' && click.problem && (
              <>
                {click.problem.next === 'review' && <Button onClick={() => void click.review()}>Read the review again</Button>}
                {click.problem.next === 'prepare' && p.action && <Button onClick={prepareClickAgain}>Start again</Button>}
                {click.problem.next === 'again' && p.action && <Button onClick={prepareClickAgain}>Do it again</Button>}
                {click.problem.next === 'retry' && p.action && <Button onClick={prepareClickAgain}>Try again</Button>}
                <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
                  {click.prepared ? 'Cancel' : 'Close'}
                </Button>
              </>
            )}
            {(click.phase === 'done' || click.phase === 'cancelled') && (
              <Button ref={cancelRef} variant={click.phase === 'done' ? 'default' : 'ghost'} onClick={() => void close()}>
                Close
              </Button>
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
            {(phase === 'failed' || phase === 'blocked' || phase === 'expired' || phase === 'changed' || phase === 'unknown' || phase === 'error') && p.action && (
              <Button onClick={again}>{phase === 'error' || phase === 'unknown' ? 'Try again' : phase === 'failed' || phase === 'expired' || phase === 'blocked' ? 'Start again' : 'Prepare again'}</Button>
            )}
            {phase === 'already' && (run.request?.state === 'submitted' || run.error?.code === 'intent.done') && p.action && <Button onClick={again}>Do it again</Button>}
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
