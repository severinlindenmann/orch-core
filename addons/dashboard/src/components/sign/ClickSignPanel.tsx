import { SignContent, SignDetails } from './SignContent'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
// The click-to-sign panel (host contract section 0.8.6 item 7): it shows ONLY the signer's View, nothing of this page's
// own preview, the host's fingerprint or a cached ticket. The risk notice is available from the One-click badge. `lines` are core's terminal
// review, already escaped, and are text here, never HTML. Sign is armed after 1 s of visible presentation.
// A word op (grant, policy, members) is confirmed by holding Sign down for HOLD_MS, not by typing the word: the word is
// not a secret (the signer only checks it matches) and the dashboard is trusted with confirmation (section 0.8.6
// item 0); the hold keeps the gesture deliberate. Every other op signs on one click.
import { Loader2, ShieldAlert } from 'lucide-react'
import { useEffect, useId, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'
import { CLICK_RISK, HOLD_MS, type ClickProblem, type ClickViewAnswer } from '@/api/click'
import { uuid8 } from '@/api/workspaces'
import { Button } from '@/components/ui/button'
import { Raw } from './visible'
import { ClickProblemBox } from './RefusalDevices'
import { useActivation, useArming } from './useArming'

function RiskLine({ text }: { text: string }) {
  return (
    <p role="note" data-testid="click-risk" className="flex items-start gap-2 rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-[13px] font-medium text-text">
      <ShieldAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
      <span>{text}</span>
    </p>
  )
}

/** The risk line alone: the workspace card and the dialog before the request exists. */
export const ClickRisk = () => <RiskLine text={CLICK_RISK} />

export const ClickBadge = () => <div className="space-y-1"><TooltipProvider><Tooltip><TooltipTrigger asChild><button type="button" className="w-fit rounded border border-border px-2 py-0.5 text-[11px] text-text-muted">One-click</button></TooltipTrigger><TooltipContent className="max-w-xs">{CLICK_RISK}</TooltipContent></Tooltip></TooltipProvider><p className="text-[12px] text-warning sm:hidden">{CLICK_RISK}</p></div>

function When({ stamp }: { stamp: string }) {
  const t = Date.parse(stamp)
  return <time dateTime={stamp}>{Number.isNaN(t) ? stamp : new Date(t).toLocaleTimeString()}</time>
}

interface Props {
  answer: ClickViewAnswer
  /** The explanation of a refusal that left the request open (try again), shown above the buttons. */
  problem: ClickProblem | null
  signing: boolean
  onSign: (word?: string) => void
  onCancel: () => void
  /** Rate limit: the time (ms since epoch) before which Sign stays off. */
  waitUntil?: number
  /** The dialog's own workspace: the devices a refusal names are looked up there, never in the View's (review I2). */
  ws?: string
}

export function ClickSignPanel({ answer, problem, signing, onSign, onCancel, waitUntil = 0, ws }: Props) {
  const { view, view_digest: digest } = answer
  // A new View (a new digest) is a new confirmation: the timer, a half-done hold and any half-begun press start over.
  return <Presented key={digest} view={view} digest={digest} problem={problem} signing={signing} onSign={onSign} onCancel={onCancel} waitUntil={waitUntil} ws={ws} />
}

function Presented({ view, digest, problem, signing, onSign, onCancel, waitUntil, ws }: Omit<Props, 'answer'> & { view: ClickViewAnswer['view']; digest: string; waitUntil: number }) {
  const armed = useArming(digest)
  const activation = useActivation(armed)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (waitUntil <= Date.now()) return
    const t = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(t)
  }, [waitUntil])
  const waiting = waitUntil > now
  const needsWord = view.confirm.kind === 'word'
  const can = armed && !signing && !waiting
  const hold = useHold(can, () => {
    if (view.confirm.kind === 'word') onSign(view.confirm.word)
  })
  const go = () => {
    if (!activation.take() || !can) return
    onSign(undefined)
  }
  const hintId = useId()
  return (
    <section data-testid="click-panel" aria-label="Review before signing" className="space-y-3">
      <ClickBadge />
      {view.content && <SignContent content={view.content} ws={ws} />}
      <p data-testid="click-custody" role="note" className="text-[13px] text-warning">
        {view.auth === 'convenience' ? 'One click on this computer, no fingerprint/passphrase' : <Raw>{view.auth}</Raw>}
      </p>
      {view.checks.filter(c => !c.clean).map((c, i) => <p key={i} role="note" className="text-[13px] text-warning">Repository <Raw>{c.repo}</Raw>: not clean</p>)}
      {view.artifacts === 'unseen' && <p data-testid="click-artifacts" role="note" className="text-[13px] text-warning">The signer did not see the artifact bytes: only their hash is signed.</p>}
      <SignDetails>
      <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-[13px]">
        <dt className="text-text-muted">Action</dt>
        <dd>
          <Raw>{view.op}</Raw>
        </dd>
        <dt className="text-text-muted">Workspace</dt>
        <dd data-testid="click-workspace" className="flex flex-wrap items-center gap-x-2">
          <span className="rounded bg-surface-3 px-1 font-mono text-text" data-ws-id>
            id {uuid8(view.workspace.id)}
          </span>
          <Raw>{view.workspace.prefix}</Raw>
          <Raw>{view.workspace.name}</Raw>
        </dd>
        <dt className="text-text-muted">Fingerprint</dt>
        <dd className="font-mono" data-testid="click-fingerprint">
          {view.fingerprint}
        </dd>
        <dt className="text-text-muted">Device</dt>
        <dd><Raw>{view.device}</Raw></dd>
        <dt className="text-text-muted">Expires</dt>
        <dd>
          <When stamp={view.expires_at} />
        </dd>
      </dl>
      {view.content && <div data-testid="click-lines" className="max-h-64 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-5 text-text">
        {view.lines.map((line, i) => (
          <div key={i} className="whitespace-pre-wrap break-all">
            {line === '' ? ' ' : line}
          </div>
        ))}
      </div>}

      {view.checks.length > 0 && (
        <ul data-testid="click-checks" className="space-y-0.5 text-[12px] text-text-muted">
          {view.checks.map((c, i) => (
            <li key={i}>
              Repository <Raw>{c.repo}</Raw>: {c.clean ? 'clean' : 'not clean'}, checked by <Raw>{c.attested_by}</Raw> at <When stamp={c.at} />
            </li>
          ))}
        </ul>
      )}
      {view.artifacts === 'checked' && (
        <p data-testid="click-artifacts" className="text-[12px] text-text-muted">
          The signer checked the artifact bytes it signs.
        </p>
      )}
      </SignDetails>
      {!view.content && <div data-testid="click-lines" className="max-h-64 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px]">{view.lines.map((line, i) => <div key={i} className="whitespace-pre-wrap break-all">{line || " "}</div>)}</div>}
      {view.confirm.kind === 'word' && (
        <p id={hintId} data-testid="click-hold-hint" className="text-[13px] text-text">
          Hold Sign to confirm <Raw>{view.confirm.word}</Raw>
        </p>
      )}
      {problem && <ClickProblemBox problem={{ ...problem, next: undefined, signedAt: undefined }} ws={ws} />}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <div className="flex items-center gap-2">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={signing}>
            Cancel
          </Button>
          {needsWord ? (
            <>
              <Button
                type="button"
                disabled={!can}
                aria-busy={signing}
                aria-describedby={hintId}
                className="relative overflow-hidden"
                {...hold.handlers}
                title={armed ? undefined : 'Sign turns on after the review has been on screen for a moment'}
              >
                {/* The fill: visual only (the progressbar below speaks); no animation for reduced motion, the hold still counts. */}
                <span
                  aria-hidden
                  data-testid="click-hold-fill"
                  className="pointer-events-none absolute inset-y-0 left-0 bg-white/25 motion-safe:transition-[width] motion-safe:duration-100 motion-safe:ease-linear motion-reduce:hidden"
                  style={{ width: `${hold.progress}%` }}
                />
                {signing ? (
                  <>
                    <Loader2 className="relative size-4 animate-spin" aria-hidden /> Signing…
                  </>
                ) : (
                  <span className="relative">Hold to sign</span>
                )}
              </Button>
              {hold.holding && (
                <span role="progressbar" aria-label="Hold to sign" aria-valuemin={0} aria-valuemax={100} aria-valuenow={hold.progress} className="sr-only">
                  {hold.progress}%
                </span>
              )}
            </>
          ) : (
            <Button
              type="button"
              disabled={!can}
              aria-busy={signing}
              onPointerDown={activation.begin}
              onKeyDown={activation.begin}
              onClick={go}
              title={armed ? undefined : 'Sign turns on after the review has been on screen for a moment'}
            >
              {signing ? (
                <>
                  <Loader2 className="size-4 animate-spin" aria-hidden /> Signing…
                </>
              ) : (
                'Sign'
              )}
            </Button>
          )}
        </div>
      </div>
    </section>
  )
}

/**
 * Press and hold for HOLD_MS: pointer down (primary button) or Space/Enter down (not an auto-repeat) starts it, and
 * only while `can` (armed, not signing, not waiting), so a press that began before arming never counts. Release, leaving
 * the button, a cancelled pointer, blur or `can` turning off end it early. `done` fires once per completed hold.
 */
function useHold(can: boolean, done: () => void) {
  const [progress, setProgress] = useState(0)
  const [holding, setHolding] = useState(false)
  const timers = useRef<{ end?: ReturnType<typeof setTimeout>; tick?: ReturnType<typeof setInterval> }>({})
  const active = useRef(false)
  const doneRef = useRef(done)
  useEffect(() => {
    doneRef.current = done
  })
  const stop = () => {
    clearTimeout(timers.current.end)
    clearInterval(timers.current.tick)
    timers.current = {}
    active.current = false
    setHolding(false)
    setProgress(0)
  }
  const start = () => {
    if (!can || active.current) return
    active.current = true
    setHolding(true)
    const began = Date.now()
    timers.current.tick = setInterval(() => setProgress(Math.min(99, Math.floor(((Date.now() - began) / HOLD_MS) * 100))), 100)
    timers.current.end = setTimeout(() => {
      stop()
      doneRef.current()
    }, HOLD_MS)
  }
  // Disarmed (blur, hidden page), signing or rate-limited mid-hold: the hold is gone, not paused.
  useEffect(() => {
    if (!can && active.current) stop()
  }, [can])
  useEffect(() => () => {
    clearTimeout(timers.current.end)
    clearInterval(timers.current.tick)
  }, [])
  const isKey = (e: KeyboardEvent) => e.key === ' ' || e.key === 'Enter'
  return {
    progress,
    holding,
    handlers: {
      onPointerDown: (e: PointerEvent) => {
        if (e.button === 0) start()
      },
      onPointerUp: stop,
      onPointerLeave: stop,
      onPointerCancel: stop,
      onBlur: stop,
      onKeyDown: (e: KeyboardEvent) => {
        if (!isKey(e)) return
        e.preventDefault() // no click from Enter, no scroll from Space
        if (!e.repeat) start()
      },
      onKeyUp: (e: KeyboardEvent) => {
        if (!isKey(e)) return
        e.preventDefault()
        stop()
      },
    },
  }
}
