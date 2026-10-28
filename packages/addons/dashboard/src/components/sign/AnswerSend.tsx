// "Send answer" for one question, in the card itself (Today card and ticket page). C2: nothing is prepared on render, on
// a pick or while the note is typed. Only a trusted press of Send answer (isTrusted, not a key repeat: trustedPress)
// prepares the request with the signer; its View then shows as the "You are answering" strip: the ticket, the question,
// the option and the note exactly as the signer will sign them, the workspace line (D6) and the technical facts behind
// the small "i". The strip's Send answer is bound to that request and its digest only and turns on after the strip has
// been visible for 1 s (useArming); a second press signs. A changed answer or Cancel cancels the open request (a
// tombstone, never signed later); the next press is a new attempt. Signing is never routed by hub mode.
// A workspace that is not click-ready gets one plain line with one button (Turn on One click), never a terminal dialog.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useRouter } from '@tanstack/react-router'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { clickReady } from '@/api/click'
import { isRealHost } from '@/api/features'
import { queries } from '@/api/queries'
import { answeredNow, signedNow } from '@/api/signedDrop'
import type { QuestionStatus } from '@/api/types'
import { workspaceOfTicket } from '@/api/workspaces'
import { useMe } from '@/app/useMe'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Button } from '@/components/ui/button'
import { Loader2 } from 'lucide-react'
import { ClickProblemBox } from './RefusalDevices'
import { ClickSignPanel, TechnicalFacts } from './ClickSignPanel'
import { SignContent, compactAnswer } from './SignContent'
import { trustedPress } from './trustedPress'
import { useActivation, useArming } from './useArming'
import { useClickSign } from './useClickSign'
import { WorkspaceLine } from './WorkspaceLine'
import { Raw } from './visible'

/** Phases in which a changed answer waits: the step in flight ends first (a prepare) or the answer is already on its way. */
const HOLD = ['preparing', 'signing', 'settling', 'done']

export function AnswerSend({
  ticket,
  question,
  option,
  text,
  canSend,
  fallback,
  onViewQuestion,
  onBusy,
}: {
  ticket: string
  question: QuestionStatus
  option?: string | null
  text?: string
  /** An answer is chosen and the ticket is loaded. */
  canSend: boolean
  /** The mockup only (no real host, no one click): its simulated dialog. A real host never uses it. */
  fallback?: () => void
  /** The question text of the signer's View, when it is shown (it replaces the card's text); null otherwise. */
  onViewQuestion?: (text: string | null) => void
  /** True while the signature is on its way or done: the owner of the answer form locks the option and the note. */
  onBusy?: (busy: boolean) => void
}) {
  const qc = useQueryClient()
  const workspaces = useQuery(queries.workspaces())
  const here = workspaceOfTicket(ticket, workspaces.data ?? [])
  const ws = here?.id
  const me = useMe(ws)
  const router = useRouter({ warn: false })
  const note = text?.trim() || undefined
  const action = useMemo<HandoffAction | null>(
    () => (option || note ? { action: 'answer', key: ticket, question: question.id, ...(option ? { option } : {}), ...(note ? { message: note } : {}) } : null),
    [ticket, question.id, option, note],
  )
  const canClick = clickReady(here)
  const many = (workspaces.data?.length ?? 0) > 1

  const click = useClickSign(ws, () => {
    // No toast: the answer is shown where it was given (the card turns into "Answered"), one message only.
    signedNow(qc, ws, ticket, question.id, question.hash)
    // The card says "You answered" at once, without waiting for the next read of the ticket.
    const by = me.data?.person
    if (by) void answeredNow(qc, ws, ticket, question.id, { option: option ?? undefined, text: note }, by)
  })
  const clickRef = useRef(click)
  clickRef.current = click
  const current = JSON.stringify(action)
  // The answer the open attempt was prepared for (set only by a trusted press).
  const startedFor = useRef<string | null>(null)

  // A changed answer: the open request is cancelled (a tombstone) and the card is back to Send answer. Nothing is prepared
  // here; the next trusted press prepares the new answer.
  useEffect(() => {
    if (startedFor.current === null || startedFor.current === current) return
    const c = clickRef.current
    if (HOLD.includes(c.phase)) return // runs again when the prepare in flight ends
    startedFor.current = null
    if (c.phase === 'idle') return
    void c.cancel().then(() => clickRef.current.reset())
  }, [current, click.phase])

  const press = (e: MouseEvent) => {
    if (!trustedPress(e.nativeEvent) || !action || !canSend) return // a render, a synthetic click or a key repeat grants nothing
    const c = clickRef.current
    startedFor.current = current
    if (c.phase === 'idle') void c.start(action)
    else if (c.phase === 'cancelled' || c.phase === 'failed') void c.again(action) // a new attempt, never the old request
  }

  const mine = startedFor.current === current
  const shown = mine && ['ready', 'signing', 'settling', 'done'].includes(click.phase) && !!click.view
  const view = shown ? click.view!.view : undefined
  const parts = view && view.confirm.kind === 'click' ? compactAnswer(view.content) : null

  const viewQuestion = parts ? parts.question.text : null
  useEffect(() => {
    onViewQuestion?.(viewQuestion)
    return () => onViewQuestion?.(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewQuestion])

  const busyNow = ['signing', 'settling', 'done'].includes(click.phase)
  useEffect(() => {
    onBusy?.(busyNow)
    return () => onBusy?.(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busyNow])

  // The View expires: the request is cancelled (never signed later). The next press prepares a new one.
  const expires = shown && click.phase === 'ready' ? Date.parse(click.view!.view.expires_at) : NaN
  useEffect(() => {
    if (Number.isNaN(expires)) return
    const t = setTimeout(() => void clickRef.current.cancel(), Math.max(0, expires - Date.now()))
    return () => clearTimeout(t)
  }, [expires])
  // The signer says the View changed: read it again (the same request, no new prepare); the arm starts over.
  const reviewedFor = useRef<string | null>(null)
  useEffect(() => {
    const id = click.prepared?.request_id ?? null
    if (click.phase !== 'failed' || click.problem?.next !== 'review' || !id || reviewedFor.current === id) return
    reviewedFor.current = id
    void click.review()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [click.phase, click.problem])

  const cancel = () => void clickRef.current.cancel()

  if (!canClick) {
    // The mockup keeps its simulated dialog; a real host has one plain line.
    if (!isRealHost() && fallback) {
      return (
        <div className="flex justify-end">
          <Button type="button" size="sm" disabled={!canSend} onClick={fallback}>
            Send answer
          </Button>
        </div>
      )
    }
    if (workspaces.isPending) {
      return (
        <div className="flex justify-end">
          <Button type="button" size="sm" disabled>
            Send answer
          </Button>
        </div>
      )
    }
    return (
      <div data-testid="answer-not-ready" className="flex flex-wrap items-center justify-end gap-2 text-[13px] text-text-muted">
        <p>One click is not ready on this computer.</p>
        {router && (
          <Button asChild size="sm" variant="outline">
            <Link to="/settings/$tab" params={{ tab: 'general' }}>
              Turn on One click
            </Link>
          </Button>
        )}
      </div>
    )
  }
  if (shown && parts && click.view) {
    return <Presented key={click.view.view_digest} click={click} many={many} onCancel={cancel} />
  }
  if (shown && click.view) {
    // A View without what the strip needs: the full review, in place, with the same arm.
    if (click.phase === 'settling')
      return (
        <p role="status" data-testid="strip-settling" className="text-[13px] text-text-muted">
          The outcome is not certain yet. Do not send again. {click.settleNote}
        </p>
      )
    if (click.phase === 'done') return null
    return <ClickSignPanel ws={ws} answer={click.view} problem={click.problem} signing={click.phase === 'signing'} onSign={() => void click.sign()} onCancel={cancel} label="Send answer" manyWorkspaces={many} />
  }
  const preparing = mine && ['preparing', 'viewing'].includes(click.phase)
  return (
    <div className="space-y-2">
      {mine && click.phase === 'failed' && click.problem && <ClickProblemBox problem={click.problem} ws={ws} />}
      {mine && click.phase === 'cancelled' && (
        <p role="status" data-testid="answer-cancelled" className="text-[13px] text-text-muted">
          Nothing was signed.
        </p>
      )}
      <div className="flex justify-end">
        <Button type="button" size="sm" disabled={!canSend || !action || preparing} onClick={press} aria-busy={preparing}>
          {preparing ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden /> Preparing…
            </>
          ) : (
            'Send answer'
          )}
        </Button>
      </div>
    </div>
  )
}

function Presented({ click, many, onCancel }: { click: ReturnType<typeof useClickSign>; many: boolean; onCancel: () => void }) {
  const answer = click.view!
  const { view, view_digest: digest } = answer
  const armed = useArming(digest) // starts at mount (a new View is a new mount) and only runs while this strip is visible
  const activation = useActivation(armed)
  const signing = click.phase === 'signing' || click.phase === 'settling'
  const can = armed && click.phase === 'ready'
  const go = (e: MouseEvent) => {
    if (!activation.take(e.nativeEvent) || !can) return // a synthetic click or a key repeat never signs
    void click.sign()
  }
  return (
    <section data-testid="answer-strip" aria-label="Your answer" className="relative space-y-3 rounded-md border border-border bg-bg p-3">
      <p role="status" className="sr-only" data-testid="strip-live">
        <Said text={liveText(click.phase, armed)} />
      </p>
      <InfoDrawer corner label="Technical details" testId="strip-technical">
        <TechnicalFacts view={view} />
      </InfoDrawer>
      <WorkspaceLine workspace={view.workspace} many={many} testId="strip-workspace" />
      <SignContent content={view.content!} ws={view.workspace?.id} compact />
      {view.checks.filter((c) => !c.clean).map((c, i) => (
        <p key={i} role="note" className="text-[13px] text-warning">
          Repository <Raw>{c.repo}</Raw>: not clean
        </p>
      ))}
      {view.artifacts === 'unseen' && (
        <p role="note" data-testid="click-artifacts" className="text-[13px] text-warning">
          The files could not be checked before signing.
        </p>
      )}
      {click.phase === 'settling' && (
        <p role="status" data-testid="strip-settling" className="text-[13px] text-text-muted">
          The outcome is not certain yet. Do not send again. {click.settleNote}
        </p>
      )}
      {click.problem && click.phase !== 'settling' && <ClickProblemBox problem={{ ...click.problem, next: undefined, signedAt: undefined }} ws={undefined} />}
      <div className="flex justify-end gap-2">
        {click.phase === 'ready' && (
          <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        )}
        <Button
          type="button"
          size="sm"
          disabled={!can}
          aria-busy={signing}
          onPointerDown={activation.begin}
          onKeyDown={activation.begin}
          onClick={go}
          title={armed ? undefined : 'Send answer turns on after the review has been on screen for a moment'}
        >
          {signing ? (
            <>
              <Loader2 className="size-4 animate-spin" aria-hidden /> Sending…
            </>
          ) : click.phase === 'done' ? (
            'Sent'
          ) : (
            'Send answer'
          )}
        </Button>
      </div>
    </section>
  )
}

/** What the live region says for a phase of the strip (nothing for a refusal: its own message is on screen). */
export function liveText(phase: string, armed: boolean): string {
  if (phase === 'signing' || phase === 'settling') return 'Sending your answer.'
  if (phase === 'done') return 'Answer sent.'
  if (phase !== 'ready') return ''
  return armed ? 'Send answer is ready.' : 'Review loaded. Send answer turns on in a moment.'
}

/** The region starts empty and is filled one tick later, so the first announcement is read by screen readers. */
function Said({ text }: { text: string }) {
  const [said, setSaid] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setSaid(text), 0)
    return () => clearTimeout(t)
  }, [text])
  return <>{said}</>
}
