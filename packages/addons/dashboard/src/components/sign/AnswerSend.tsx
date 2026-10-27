// "Send answer" for one question, in the card itself. In one-click (convenience) custody picking an option prepares the
// request with the signer and shows ITS View as a "You are sending" strip: the ticket, the question, the option and the
// note exactly as the signer will sign them. The button is bound to that request and its digest only and turns on after the
// strip has been visible for 1 s (useArming); every new View (another option, another note, a new request after expiry)
// starts the timer over. Anything else (passphrase, Touch ID, terminal hand-off, a View without the fields the strip needs,
// a refusal) is the existing two-step dialog: the button calls `fallback`. Nothing here signs without a shown View.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { clickReady } from '@/api/click'
import { handoffMode } from '@/api/features'
import { hubOnly } from '@/api/hubsign'
import { queries } from '@/api/queries'
import { signedNow } from '@/api/signedDrop'
import type { QuestionStatus } from '@/api/types'
import { uuid8, workspaceOfTicket } from '@/api/workspaces'
import { Button } from '@/components/ui/button'
import { Loader2 } from 'lucide-react'
import { ClickProblemBox } from './RefusalDevices'
import { TechnicalFacts } from './ClickSignPanel'
import { SignContent, compactAnswer } from './SignContent'
import { useSignMethods } from './SignMethods'
import { useActivation, useArming } from './useArming'
import { useClickSign } from './useClickSign'
import { Raw } from './visible'

const BUSY = ['preparing', 'viewing', 'signing', 'settling', 'done']
const NOTE_DEBOUNCE_MS = 400

export function AnswerSend({
  ticket,
  question,
  option,
  text,
  canSend,
  fallback,
  onViewQuestion,
  onBusy,
  type = 'button',
}: {
  ticket: string
  question: QuestionStatus
  option?: string | null
  text?: string
  /** An answer is chosen and the ticket is loaded. */
  canSend: boolean
  /** The existing dialog (two steps), for every case the strip does not serve. */
  fallback: () => void
  /** The question text of the signer's View, when it is shown (it replaces the card's text); null otherwise. */
  onViewQuestion?: (text: string | null) => void
  /** True while the signature is on its way or done: the owner of the answer form locks the option and the note. */
  onBusy?: (busy: boolean) => void
  type?: 'button' | 'submit'
}) {
  const qc = useQueryClient()
  const workspaces = useQuery(queries.workspaces())
  const here = workspaceOfTicket(ticket, workspaces.data ?? [])
  const ws = here?.id
  const methods = useSignMethods(ws)
  const note = text?.trim() || undefined
  const action = useMemo<HandoffAction | null>(
    () => (option || note ? { action: 'answer', key: ticket, question: question.id, ...(option ? { option } : {}), ...(note ? { message: note } : {}) } : null),
    [ticket, question.id, option, note],
  )
  // The way this workspace signs, as the dialog works it out (no phone for an answer): inline only for one click.
  const canClick = clickReady(here) && !hubOnly(here)
  const { allowed, preference } = methods
  const usable = (m: string) => m !== 'click' || canClick
  const preferred = preference?.method
  const fallbackMethod = preference?.fallback
  const method = allowed
    ? preferred && allowed.includes(preferred) && usable(preferred) ? preferred
      : fallbackMethod && allowed.includes(fallbackMethod) && usable(fallbackMethod) ? fallbackMethod
        : !preferred && canClick && allowed.includes('click') ? 'click' : 'terminal'
    : canClick ? 'click' : 'terminal'
  // Needs a picked option: a note alone is the dialog's (no request per keystroke).
  const inline = handoffMode(ws) && !methods.loading && !!action && !!option && method === 'click' && !workspaces.isPending

  const click = useClickSign(ws, () => {
    // No toast: the answer is shown where it was given (the card turns into "Answered"), one message only.
    signedNow(qc, ws, ticket, question.id, question.hash)
  })
  const clickRef = useRef(click)
  clickRef.current = click
  const [nonce, setNonce] = useState(0)
  const current = `${JSON.stringify(action)}#${nonce}`
  const started = useRef<string | null>(null)
  const lastText = useRef(note)

  // Prepare on pick: the request in the signer always belongs to exactly the answer on screen.
  useEffect(() => {
    const c = clickRef.current
    if (!inline || !action) {
      if (c.prepared && !BUSY.includes(c.phase)) void c.cancel().then(() => clickRef.current.reset())
      started.current = null
      return
    }
    if (BUSY.includes(c.phase)) return // an earlier step is in flight: this effect runs again when it ends
    if (started.current === current) return
    const delay = lastText.current !== note ? NOTE_DEBOUNCE_MS : 0
    lastText.current = note
    const t = setTimeout(async () => {
      const k = clickRef.current
      if (k.prepared && k.phase !== 'idle') {
        await k.cancel() // no earlier request stays open
        clickRef.current.reset()
        return // idle again: the effect runs once more and starts the new one
      }
      if (k.phase !== 'idle') {
        k.reset()
        return
      }
      started.current = current
      void k.start(action)
    }, delay)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inline, current, click.phase])

  // The strip stays mounted (disabled) while the signature is on its way and after it, never flashing away mid-sign.
  const shown = inline && started.current === current && ['ready', 'signing', 'settling', 'done'].includes(click.phase) && !!click.view
  const content = shown ? click.view!.view.content : undefined
  const parts = shown && click.view!.view.confirm.kind === 'click' ? compactAnswer(content) : null

  const viewQuestion = parts ? parts.question.text : null
  useEffect(() => {
    onViewQuestion?.(viewQuestion)
    return () => onViewQuestion?.(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewQuestion])

  const busyNow = inline && ['signing', 'settling', 'done'].includes(click.phase)
  useEffect(() => {
    onBusy?.(busyNow)
    return () => onBusy?.(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busyNow])

  // The View expires, or the signer says it changed: a new request, a new View, the timer starts over.
  const expires = shown && click.phase === 'ready' ? Date.parse(click.view!.view.expires_at) : NaN
  useEffect(() => {
    if (Number.isNaN(expires)) return
    const t = setTimeout(() => setNonce((n) => n + 1), Math.max(0, expires - Date.now()))
    return () => clearTimeout(t)
  }, [expires])
  const autoFor = useRef<string | null>(null)
  useEffect(() => {
    const id = click.prepared?.request_id ?? null
    if (click.phase !== 'failed' || !click.problem || !id || autoFor.current === id) return
    if (click.problem.next === 'review') {
      autoFor.current = id
      void click.review()
    } else if (click.problem.next === 'prepare' || click.problem.next === 'again') {
      autoFor.current = id
      setNonce((n) => n + 1)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [click.phase, click.problem])

  const open = async () => {
    // Never sign from here: the dialog takes over, and an open request here is cancelled first.
    if (click.prepared && !BUSY.includes(click.phase)) await click.cancel()
    fallback()
  }

  if (!inline) {
    return (
      <div className="flex justify-end">
        <Button type={type} size="sm" disabled={!canSend} onClick={type === 'submit' ? undefined : fallback}>
          Send answer
        </Button>
      </div>
    )
  }
  if (shown && parts && click.view) {
    return <Presented key={click.view.view_digest} click={click} showWorkspace={(workspaces.data?.length ?? 0) > 1} />
  }
  const preparing = !['ready', 'failed', 'cancelled'].includes(click.phase) || started.current !== current
  return (
    <div className="space-y-2">
      {click.phase === 'failed' && click.problem && <ClickProblemBox problem={click.problem} ws={ws} />}
      <div className="flex justify-end">
        {/* A View without what the strip needs, or a refusal: the full review dialog, the same as before. */}
        <Button type="button" size="sm" disabled={!canSend || preparing} onClick={() => void open()} aria-busy={preparing}>
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

function Presented({ click, showWorkspace }: { click: ReturnType<typeof useClickSign>; showWorkspace: boolean }) {
  const answer = click.view!
  const { view, view_digest: digest } = answer
  const armed = useArming(digest) // starts at mount (a new View is a new mount) and only runs while this strip is visible
  const activation = useActivation(armed)
  const signing = click.phase === 'signing' || click.phase === 'settling'
  const can = armed && click.phase === 'ready'
  const go = () => {
    if (!activation.take() || !can) return
    void click.sign()
  }
  return (
    <section data-testid="answer-strip" aria-label="You are answering" className="space-y-3 rounded-md border border-border bg-bg p-3">
      <p role="status" className="sr-only" data-testid="strip-live">
        <Said text={liveText(click.phase, armed)} />
      </p>
      <h3 className="text-[12px] font-medium text-text-muted">You are answering</h3>
      {showWorkspace && view.workspace?.name && (
        <p data-testid="strip-workspace" className="text-[12px] text-text-muted">
          {/* the name is unsigned (D6): never alone, always after the prefix and the id tail */}
          Workspace <Raw>{view.workspace.prefix}</Raw> · <span className="font-mono" data-ws-id>{uuid8(view.workspace.id)}</span> · <bdi><Raw>{view.workspace.name}</Raw></bdi>
        </p>
      )}
      <SignContent content={view.content!} ws={view.workspace?.id} technical={<TechnicalFacts view={view} />} compact />
      {click.phase === 'settling' && (
        <p role="status" data-testid="strip-settling" className="text-[13px] text-text-muted">
          The outcome is not certain yet. Do not send again. {click.settleNote}
        </p>
      )}
      {click.problem && click.phase !== 'settling' && <ClickProblemBox problem={{ ...click.problem, next: undefined, signedAt: undefined }} ws={undefined} />}
      <div className="flex justify-end">
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
