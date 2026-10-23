import { api } from '@/api/client'
import { useSignMethods } from './SignMethods'
import { WhatNext } from './SignAside'
import { storyOf } from './story'
import { methodLabel, type SignContent } from '@/api/signing'
// Core's signing dialog in hand-off mode. Same place, same core words in the title and the buttons as the simulated
// Touch ID dialog it replaces, but the dashboard does not sign: it prepares a request with the host, shows the host's
// material, the fingerprint, the code and the exact terminal command, and follows the outcome (design 4).
import { Loader2 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import type { HandoffAction, HandoffRequest, HandoffState } from '@/api/handoff'
import { Button } from '@/components/ui/button'
import { lastTrigger, SignShell } from './SignShell'
import { queries } from '@/api/queries'
import { sharesPrefix, uuid8 } from '@/api/workspaces'
import { useWorkspace } from '@/app/workspace'
import { alreadyDetails, alreadySigned, clickEligible, clickReady } from '@/api/click'
import { refusalOf, refusalSentence } from '@/api/refusals'
import { addFirst, missingOf } from '@/api/gates'
import { ClickProblemBox, RefusalDevices, TechnicalDetails } from './RefusalDevices'
import { ClickSignPanel } from './ClickSignPanel'
import { useClickSign } from './useClickSign'
import { HandoffPanel } from './HandoffPanel'
import { HubSignPanel, NotFromHub } from './HubSignPanel'
import { hubOnly, hubRequestOf } from '@/api/hubsign'
import { Inline, Raw } from './visible'
import { failedSubmit, useHandoff, type HandoffPhase } from './useHandoff'
import { fmtClock } from '@/lib/time'

interface Words {
  title: string
  body: string
  tone?: 'ok' | 'warn'
}

/** What each end state says, in core's words. */
function words(phase: HandoffPhase, error: string | null, code?: string, newTicket = false): Words | null {
  switch (phase) {
    case 'cancelled':
      return { title: 'Cancelled', body: 'Nothing was signed. This happens when you cancel in the terminal or the word you typed there was not the one asked for. Try again to get a fresh request.' }
    case 'expired':
      if (code === 'intent.expired') return { title: 'This attempt is too old; start again', body: error ?? '', tone: 'warn' }
      return { title: 'Expired', body: 'The request ran out of time before it was signed. Prepare it again to get a new code.', tone: 'warn' }
    case 'already':
      return { title: 'Already done', body: code === 'intent.done' || code === 'addon.already' ? error ?? '' : 'The host says this is already in place, so there is nothing to sign. The page shows the current state. If you meant to do it again, say so.' }
    case 'changed':
      return { title: 'Changed since you started', body: 'The ticket changed. The page now shows the current state. Prepare again to sign what is there now.', tone: 'warn' }
    case 'unknown':
      return { title: 'Unknown outcome', body: 'The dashboard lost track of this request (an answer was lost). Try again asks the host for the same request with the same key: it returns the request it already made, in its current state, and never a second one. The page shows the current state.', tone: 'warn' }
    case 'failed':
      if (code === 'sequence.no_access') return { title: newTicket ? 'Ticket created; agent access not confirmed yet' : 'Task created; agent access not confirmed yet', body: error ?? '', tone: 'warn' }
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

/** Core's review has a headline and nothing under it (a switch with no fields): the page's own plain lines fill the gap. */
function isBareContent(c: SignContent | null | undefined): boolean {
  return !!c && !c.summary && !c.sections?.length && !c.acceptance?.length && !c.tasks?.length && !c.fields_changed?.length && !c.note?.length && !c.notes?.length && !c.ticket
}

export interface SignPopoverProps {
  /** Core's title: "Approve plan", "Comment", "Close DEMO-0043". */
  title: string
  ws: string | undefined
  /** What to prepare. Null while the form is not complete (the button stays off). */
  action: HandoffAction | null
  /** Prepare as soon as the review opens (an action with nothing to fill in). The press that opened it is the intent. */
  autoStart?: boolean
  /** The page starts an agent after this signature (with access already there or given by it): the steps say so. */
  startsAgent?: boolean
  /** Button that starts the preparation when there is a form: the verb, in core's words. */
  confirmLabel?: string
  /** Why the button is off, shown beside it. */
  hint?: string | null
  /** The form, before the request exists. */
  children?: ReactNode
  /** Choices that are part of what is signed (agent access length): shown above the review in every state but signing. A change of `action` needs a new key on this component, which prepares again. */
  controls?: ReactNode
  /** Exact action summary, kept visible while preparing and reviewing. */
  summary?: ReactNode
  /** Said above the panel while it is open: a warning that the page changed under it. */
  warning?: (request: HandoffRequest) => string | null
  /** Said under the review and outside the signed content: what happens next that this signature does NOT cover. */
  unsigned?: ReactNode
  /** The signer's event is in the log. The review closes by itself right after. */
  onSettled?: (phase: HandoffPhase, state: HandoffState | null, request: HandoffRequest | null) => void
  onPending?: (pending: boolean) => void
  /** True while the signature itself is being made. With this set the review folds into the anchor button, which says "Signing…". */
  onSigning?: (signing: boolean) => void
  /** A click signature was rejected or cancelled. */
  onInterrupted?: () => void
  /** The ONE toast after a signature (default: "<title>: signed"). */
  doneToast?: string
  /** The button this review belongs to. Default: the button that was pressed last. */
  anchor?: HTMLElement | null
  onClose: () => void
  initialFocus?: () => HTMLElement | null
}

export function SignPopover(p: SignPopoverProps) {
  const [anchor] = useState<HTMLElement | null>(() => (p.anchor !== undefined ? p.anchor : lastTrigger()))
  const run = useHandoff(p.ws, (phase, state, request) => {
    if (phase === 'done') toast.success(p.doneToast ?? `${p.title}: signed`)
    p.onSettled?.(phase, state, request)
  })
  const { phase } = run
  const { workspaces } = useWorkspace()
  // The workspace's card (the signer's flag) comes from the provider, or from the list when none wraps this dialog.
  const known = useQuery(queries.workspaces())
  const here = workspaces.find((w) => w.id === p.ws) ?? known.data?.find((w) => w.id === p.ws)
  // V3 C11 (D78/D81): in full hub mode no local key signs; the dialog shows the hub panel and prepares nothing here.
  const viaHub = hubOnly(here)
  // Click-to-sign (H5c): offered where the signer says the flag is on and it can sign, for the actions it serves.
  const click = useClickSign(p.ws, (_phase, state, request) => {
    toast.success(p.doneToast ?? `${p.title}: signed`)
    p.onSettled?.('done', state, request)
  })
  const methods = useSignMethods(p.ws)
  const story = storyOf(p.action, { startsAgent: p.startsAgent })
  // One sign button. The terminal is only the forced fallback: an op the browser cannot sign, or a workspace where
  // One click is off or not allowed.
  const terminalRequired = p.action?.action === 'settings_set' && !!p.action.sign_methods
  const canClick = !terminalRequired && clickReady(here) && clickEligible(p.action) && (!methods.allowed || methods.allowed.includes('click'))
  const method: 'click' | 'terminal' = canClick ? 'click' : 'terminal'
  const startMethod = (retryFloor = 0) => {
    if (!p.action) return
    if (method === 'click') void click.start(p.action, retryFloor)
    else void run.prepare(p.action, retryFloor)
  }
  useEffect(() => {
    if (click.phase === 'cancelled' || (click.phase === 'failed' && click.problem?.next !== 'check')) p.onInterrupted?.()
  }, [click.phase, click.problem, p.onInterrupted])
  // A one-click sequence that stopped part way: ask the server which ticket exists and how far the run got, once, and
  // tell the caller (a form that must not offer a second ticket for the same typed text). Never read from the error text.
  const reportedStop = useRef<string | null>(null)
  useEffect(() => {
    const prepared = click.prepared
    if (click.phase !== 'failed' || click.problem?.next !== 'open' || !prepared || !p.ws || reportedStop.current === prepared.request_id) return
    reportedStop.current = prepared.request_id
    void api.stateClick(p.ws, prepared.request_id).then((got) => {
      if (!got.key || !got.progress) return
      const state: HandoffState = { id: prepared.request_id, state: 'submitted', op: prepared.op, key: got.key, expires_at: prepared.expires_at, event_ids: [], seq: null, progress: got.progress }
      const request = { id: prepared.request_id, code: null, expires_at: prepared.expires_at, state: 'submitted', op: prepared.op, key: got.key } as unknown as HandoffRequest
      p.onSettled?.('failed', state, request)
    }, () => {})
  }, [click.phase, click.problem, click.prepared, p.ws, p.onSettled])
  const clickOpen = click.phase !== 'idle'
  const [waitUntil, setWaitUntil] = useState(0)
  const shared = !!here && workspaces.some((o) => o.id !== here.id && sharesPrefix(o, here))
  const started = useRef(false)
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    // Not before the list of workspaces and the signing settings are known: whether this one offers Sign decides how the
    // review starts. An action with nothing to fill in prepares at once (the press that opened it is the intent), so the
    // person never sees a Sign button before the host's content (WYSIWYS).
    if (!viaHub && p.autoStart && p.action && !started.current && p.ws && !known.isPending && !methods.loading) {
      started.current = true
      // A click request that ended earlier (cancelled, expired) must not block a fresh open: this open is a new intent.
      if (method === 'click') void click.start(p.action, 0, true)
      else void run.prepare(p.action)
    }
    // Once per opening: a changed action object is not a new press.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viaHub, p.autoStart, p.ws, known.isPending, canClick, methods.loading, method])

  // Switching workspace cancels the open request at the host; the review belongs to the old workspace and closes.
  useEffect(() => {
    if (!run.switched) return
    toast(`${p.title}: cancelled, you switched workspace. Nothing was signed.`)
    p.onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.switched])

  // The click request lives in this review too: switching workspace cancels it like the hand-off.
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

  // Signed: the toast has said it; the review has nothing more to show.
  useEffect(() => {
    if (click.phase === 'done' || phase === 'done') p.onClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [click.phase, phase])

  const clickLive = ['preparing', 'viewing', 'ready', 'signing', 'settling'].includes(click.phase)
  const open = phase === 'preparing' || phase === 'waiting' || clickLive
  useEffect(() => {
    p.onPending?.(open)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  // The press on Sign folds the review into the button; an error opens it again with the error inside.
  const signing = click.phase === 'signing'
  useEffect(() => {
    p.onSigning?.(signing)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signing])
  useEffect(() => () => p.onSigning?.(false), [])  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (click.problem?.waitMs) setWaitUntil(Date.now() + click.problem.waitMs)
  }, [click.problem])

  const close = async () => {
    if (phase === 'waiting' || phase === 'invalid') await run.cancel()
    // Only Cancel cancels. When the outcome is unknown, closing must not cancel: the signature may be on its way.
    if (click.prepared && click.problem?.next !== 'check' && !['done', 'cancelled', 'idle'].includes(click.phase)) await click.cancel()
    p.onClose()
  }
  const prepareClickAgain = async () => {
    if (!p.action) return
    // The switch moved since the page chose: a NEW request follows the host's current answer (an open request keeps
    // the method it was made for and is cancelled, never signed another way).
    if (method !== 'click') {
      if (click.prepared) await click.cancel()
      click.reset()
      startMethod(Math.max(run.retry, click.retry) + 1)
      return
    }
    void click.again(p.action) // cancels an open request first: no leaked slot
  }
  const again = () => {
    if (!p.action) return
    if (method === 'click' && phase === 'error' && run.error?.code === 'sign.click_only') {
      // The switch moved after the page chose the terminal: a NEW request, the way the host answers now.
      run.reset()
      startMethod(Math.max(run.retry, click.retry) + 1)
      return
    }
    run.reset(phase === 'unknown' || phase === 'error') // the same key where the host may already have the request
    void run.prepare(p.action)
  }
  // H12d-2: a click-to-sign refusal (or a reader declaration that did not verify) is said in plain words; the host's own
  // message follows as text.
  const refusal = refusalOf(run.error)
  // B11 (#14): an incomplete gate is said in words ("Add Context and Out of scope first."), never as the raw code
  const lacking = run.error?.code === 'gate.incomplete' ? missingOf(run.error.message) : null
  const errorText = lacking?.length ? addFirst(lacking) : run.error?.message ?? null
  const end = words(phase, run.error?.code === 'intent.done' ? alreadySigned(run.error) : refusal ? refusalSentence(refusal) : errorText, run.error?.code, (run.request?.material.sequence?.intent as { kind?: unknown } | undefined)?.kind === 'new_ticket')
  // a CLI pointer ("orch describe approve") means nothing on this page
  const cliOnly = (v: unknown) => run.error?.code === 'gate.incomplete' && typeof v === 'string' && /\borch describe\b/.test(v)

  if (viaHub) {
    const request = hubRequestOf(p.action)
    return (
      <SignShell anchor={anchor} open onDismiss={p.onClose} title={<Inline>{p.title}</Inline>} description="Only you can approve this. Agents cannot." showClose onOpenAutoFocus={(e) => e.preventDefault()}>
        {request && p.ws ? <HubSignPanel ws={p.ws} request={request} /> : <NotFromHub />}
        <div className="flex justify-end gap-2">
          <Button variant="outline" ref={cancelRef} onClick={p.onClose}>
            Close
          </Button>
        </div>
      </SignShell>
    )
  }

  const title = <Inline>{click.view?.view.content?.title ?? click.view?.view.content?.headline ?? click.view?.view.headline ?? run.request?.content?.title ?? run.request?.content?.headline ?? run.request?.headline ?? p.title}</Inline>
  const description = (
    <>
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
      <p>
        {(click.view?.view.content || run.request?.content) ? (click.view?.view.content?.subline ?? run.request?.content?.subline ?? 'Review what you’re approving.') : clickOpen
          ? 'Only you can approve this. Agents cannot.'
          : phase === 'idle'
            ? method === 'click'
              ? 'Sign with one click on this computer.'
              : 'This one is signed in your terminal. This page only shows the request.'
            : 'Only you can approve this. Agents cannot.'}
      </p>
    </>
  )

  return (
    <SignShell
      anchor={anchor}
      open={!(p.onSigning && signing)}
      onDismiss={() => phase !== 'preparing' && click.phase !== 'preparing' && click.phase !== 'signing' && void close()}
      title={title}
      description={description}
      showClose={!(end || phase === 'waiting')}
      onOpenAutoFocus={(e) => {
        e.preventDefault()
        ;(p.initialFocus?.() ?? cancelRef.current)?.focus()
      }}
    >
      {p.controls && !signing && phase !== 'waiting' && <div data-testid="sign-controls">{p.controls}</div>}
      {(!(click.view?.view.content || run.request?.content) || isBareContent(click.view?.view.content ?? run.request?.content)) && p.summary}
      {phase === 'idle' && !clickOpen && p.children}

      {(click.phase === 'preparing' || click.phase === 'viewing') && (
        <p role="status" className="flex items-center gap-2 text-[13px] text-text-muted">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          {click.phase === 'preparing' ? 'Preparing the request…' : 'Reading the review…'}
        </p>
      )}

      {(click.phase === 'ready' || click.phase === 'signing') && click.view && (
        <ClickSignPanel
          ws={p.ws}
          answer={click.view}
          problem={click.problem}
          signing={click.phase === 'signing'}
          onSign={(word) => void click.sign(word)}
          onCancel={() => void close()}
          waitUntil={waitUntil}
          story={story}
        />
      )}
      {click.phase === 'ready' && click.problem?.stale && p.action && (
        <Button data-testid="click-try-again" onClick={() => void prepareClickAgain()}>Try again</Button>
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

      {click.phase === 'failed' && click.problem && <ClickProblemBox problem={click.problem} ws={p.ws} />}

      {click.phase === 'cancelled' && (
        <div role="status" data-click-end="cancelled" className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
          <p className="font-medium text-text">Cancelled</p>
          <p className="text-text-muted">Nothing was signed. Try again to get a fresh request.</p>
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
              Refused: {failedSubmit(run.state.error.code)} The request stays open until {fmtClock(run.request.expires_at)}.
            </p>
          )}
          <HandoffPanel request={run.request} workspace={here ?? null} />
          {run.state?.progress && (
            <p role="status" data-testid="sequence-progress" className="text-[13px] text-text-muted">
              Signing in your terminal: {run.state.progress.done} of {run.state.progress.of} steps are in the ticket.
            </p>
          )}
        </>
      )}

      {end && (
        <div role={end.tone === 'warn' ? 'alert' : 'status'} data-handoff-end={phase} className="rounded-md border border-border bg-bg px-3 py-2 text-[13px]">
          <p className="font-medium text-text">{end.title}</p>
          <p title={run.error?.signed_at ?? undefined} className="text-text-muted"><Raw>{end.body}</Raw></p>
          {run.error?.code === 'intent.done' && alreadyDetails(run.error) && <TechnicalDetails text={alreadyDetails(run.error)!} />}
          {refusal && p.ws && <RefusalDevices ws={p.ws} ids={refusal.devices} />}
          {refusal && run.error?.message && run.error.message !== run.error.code && (
            <p data-testid="refusal-host-message" className="text-text-faint">
              The host says: <Raw>{run.error.message}</Raw>
            </p>
          )}
          {run.error?.fix && !cliOnly(run.error.fix) && <p><Raw>{typeof run.error.fix === 'string' ? run.error.fix : JSON.stringify(run.error.fix)}</Raw></p>}
          {run.error?.hint && !cliOnly(run.error.hint) && <p className="text-text-faint"><Raw>{run.error.hint}</Raw></p>}
        </div>
      )}

      {p.unsigned && !end && click.phase !== 'done' && <div data-testid="handoff-unsigned">{p.unsigned}</div>}

      {phase === 'waiting' && story && <WhatNext story={story} />}
      <div className="flex flex-wrap items-center gap-2 sm:justify-between">
        <span aria-live="polite" className="text-[12px] text-text-muted">
          {phase === 'idle' && p.hint}
          {phase === 'waiting' && 'Waiting for your signature'}
        </span>
        <div className="flex flex-wrap gap-2">
          {phase === 'idle' && !clickOpen && !(p.autoStart && p.action) && (
            <>
              <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
                Cancel
              </Button>
              <Button onClick={() => startMethod()} disabled={!p.action || !p.ws || !!p.hint || methods.loading || (method === 'click' && !canClick)}>
                {method === 'click' ? (!p.autoStart && p.confirmLabel ? p.confirmLabel : 'Sign') : (methods.allowed ? methodLabel.terminal : (p.confirmLabel ?? p.title))}
              </Button>
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
              {click.problem.next === 'prepare' && p.action && <Button onClick={() => void prepareClickAgain()}>Start again</Button>}
              {click.problem.next === 'again' && p.action && <Button onClick={() => void prepareClickAgain()}>Do it again</Button>}
              {click.problem.next === 'retry' && p.action && <Button onClick={() => void prepareClickAgain()}>Try again</Button>}
              <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
                {click.prepared ? 'Cancel' : 'Close'}
              </Button>
            </>
          )}
          {click.phase === 'cancelled' && p.action && <Button onClick={() => void prepareClickAgain()}>Try again</Button>}
          {click.phase === 'cancelled' && (
            <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
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
          {(phase === 'failed' || phase === 'blocked' || phase === 'expired' || phase === 'changed' || phase === 'unknown' || phase === 'error' || phase === 'cancelled') && p.action && run.error?.code !== 'sequence.no_access' && (
            <Button onClick={again}>{phase === 'error' || phase === 'unknown' || phase === 'cancelled' ? 'Try again' : phase === 'failed' || phase === 'expired' || phase === 'blocked' ? 'Start again' : 'Prepare again'}</Button>
          )}
          {phase === 'already' && (run.request?.state === 'submitted' || run.error?.code === 'intent.done') && p.action && <Button onClick={again}>Do it again</Button>}
          {end && (
            <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>
              Close
            </Button>
          )}
        </div>
      </div>
    </SignShell>
  )
}
