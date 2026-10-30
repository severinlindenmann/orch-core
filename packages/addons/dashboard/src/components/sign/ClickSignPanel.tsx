import { usePicturesOk } from './Pictures'
import { SignContent, SignDetails } from './SignContent'
// The click-to-sign panel (host contract section 0.8.6 item 7): it shows ONLY the signer's View, nothing of this page's
// own preview, the host's fingerprint or a cached ticket. `lines` are core's terminal
// review, already escaped, and are text here, never HTML. C3 (usePressSign): a View prepared by a trusted press and
// without risk lines signs at the end of a 1 s arm (the button shows the signer's headline and a progress fill; any
// press, Esc, a press elsewhere, blur or a hidden page cancels); risk lines are shown under the button and need a second
// press 1 s after they are on screen; any other View signs on a press after 1 s of visible presentation.
// Every op signs on one plain click after the review; nothing is typed. The risk notice lives in Settings; the custody is a technical fact under Technical details.
import { Loader2, ShieldAlert } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { ARM_MS, CLICK_RISK, contractViewOk, riskLinesOf, type ClickProblem, type ClickViewAnswer } from '@/api/click'
import { Button } from '@/components/ui/button'
import { Inline, Raw } from './visible'
import { ClickProblemBox } from './RefusalDevices'
import { ARM_INSIDE, RISK_LINE, usePressSign } from './useArming'
import type { SignStory } from './story'
import { AgentAccess, WhatNext } from './SignAside'
import { alsoSigned } from './alsoSigned'
import { fmtClock } from '@/lib/time'
import { SignFooter, STICKY_FOOTER } from './SignShell'
import { WorkspaceLine } from './WorkspaceLine'

function RiskLine({ text }: { text: string }) {
  return (
    <p role="note" data-testid="click-risk" className="flex items-start gap-2 rounded-md border border-warning/50 border-l-[3px] border-l-risk-bar bg-warning-soft px-3 py-2 text-[13px] font-medium text-text">
      <ShieldAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
      <span>{text}</span>
    </p>
  )
}

/** The risk line alone: the workspace card and the dialog before the request exists. */
export const ClickRisk = () => <RiskLine text={CLICK_RISK} />

function When({ stamp }: { stamp: string }) {
  const t = Date.parse(stamp)
  return <time dateTime={stamp}>{Number.isNaN(t) ? stamp : fmtClock(stamp)}</time>
}

interface Props {
  answer: ClickViewAnswer
  /** The explanation of a refusal that left the request open (try again), shown above the buttons. */
  problem: ClickProblem | null
  signing: boolean
  onSign: () => void
  onCancel: () => void
  /** Rate limit: the time (ms since epoch) before which Sign stays off. */
  waitUntil?: number
  /** The dialog's own workspace: the devices a refusal names are looked up there, never in the View's (review I2). */
  ws?: string
  /** What this signature leads to (button label, steps, access): derived from the action. */
  story?: SignStory | null
  /** The button's words when the caller names the action ("Sign and turn on"); else the story's, else "Sign". */
  label?: string
  /** A field the review needs before Sign (the hub step's backup key), shown above the buttons. */
  extra?: ReactNode
  /** False while that field is empty: Sign stays off. */
  ready?: boolean
  /** The person has more than one workspace: the review names the one it signs in (D6, WorkspaceLine). */
  manyWorkspaces?: boolean
  /**
   * C3: this View is the first of a request prepared by a trusted press here (`ClickRun.fresh`). Without risk lines it
   * runs the 1 s arm and asks for the signature at its end; otherwise (and by default) a press after 1 s signs.
   */
  pressed?: boolean
  /** The arm was cancelled (Esc, a second press, a press elsewhere, blur, hidden page). Default: `onCancel`. */
  onDisarm?: () => void
}

export function ClickSignPanel({ answer, problem, signing, onSign, onCancel, waitUntil = 0, ws, story, label, extra, ready = true, manyWorkspaces = false, pressed = false, onDisarm }: Props) {
  const { view, view_digest: digest } = answer
  // A new View (a new digest) is a new confirmation: the timer, a half-done hold and any half-begun press start over.
  return <Presented key={digest} view={view} digest={digest} problem={problem} signing={signing} onSign={onSign} onCancel={onCancel} waitUntil={waitUntil} ws={ws} story={story} label={label} extra={extra} ready={ready} manyWorkspaces={manyWorkspaces} pressed={pressed} onDisarm={onDisarm} />
}

/** The 1 s progress fill of the arm, inside the button (restarts with its `key`). */
export function ArmFill({ ms = ARM_MS }: { ms?: number }) {
  const [full, setFull] = useState(false)
  useEffect(() => {
    const t = setTimeout(() => setFull(true), 0)
    return () => clearTimeout(t)
  }, [])
  return <span aria-hidden data-testid="sign-progress" className="pointer-events-none absolute inset-y-0 left-0 bg-white/25 transition-[width] ease-linear" style={{ width: full ? '100%' : '0%', transitionDuration: `${ms}ms` }} />
}

/** C3: the signer's headline (its own words and the ticket key) and, after it, small and muted, its `headline_detail`
 * (the capped, quoted free text: never read as part of the headline). */
export function SignerHeadline({ view, fallback }: { view: Pick<ClickViewAnswer['view'], 'headline' | 'headline_detail'>; fallback: ReactNode }) {
  const detail = typeof view.headline_detail === 'string' && view.headline_detail.trim() !== '' ? view.headline_detail : null
  return (
    <>
      {view.headline ? <Inline>{view.headline}</Inline> : fallback}
      {view.headline && detail && (
        <span data-testid="sign-headline-detail" className="ml-1.5 text-[11px] font-normal opacity-75">
          <Inline>{detail}</Inline>
        </span>
      )}
    </>
  )
}

/** C3: the signer's risk lines, under the button, never folded. Each line carries `RISK_LINE` (counted before a press counts). */
export function RiskLines({ lines, box, testId = 'sign-risk-lines', danger = false }: { lines: readonly string[]; box?: (el: HTMLElement | null) => void; testId?: string; /** A destructive or data-leaving action: the same line in red (#421). */ danger?: boolean }) {
  // Sign only counts once the whole warning is on screen: bring it into view by itself when it appears (run 14: Sign
  // looked stuck while the line sat below the fold).
  const ref = useCallback(
    (el: HTMLElement | null) => {
      box?.(el)
      if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'nearest' })
    },
    [box],
  )
  if (lines.length === 0) return null
  return (
    <ul ref={ref} role="note" data-testid={testId} data-tone={danger ? 'danger' : 'warning'} className={danger ? 'space-y-1 rounded-md border border-danger/50 border-l-[3px] border-l-risk-danger-bar bg-danger-soft px-3 py-2 text-[13px] font-medium text-text' : 'space-y-1 rounded-md border border-warning/50 border-l-[3px] border-l-risk-bar bg-warning-soft px-3 py-2 text-[13px] font-medium text-text'}>
      {lines.map((line, i) => (
        <li key={i} {...{ [RISK_LINE]: '' }} className="flex items-start gap-2">
          <ShieldAlert className={danger ? 'mt-0.5 size-4 shrink-0 text-danger' : 'mt-0.5 size-4 shrink-0 text-warning'} aria-hidden />
          <span><Inline>{line}</Inline></span>
        </li>
      ))}
    </ul>
  )
}

/** What the one "Technical details" expander of the review holds besides the content's own ids (action, workspace, fingerprint, device, the signer's lines and checks). */
export function TechnicalFacts({ view, action }: { view: ClickViewAnswer['view']; /** C3: the press that starts a new arm after "i" stopped one. */ action?: ReactNode }) {
  return (
    <>
      {action}
      {view.info_lines?.map((line, i) => <p key={i} data-testid="click-info-line"><Inline>{line}</Inline></p>)}
      {riskLinesOf(view).length > 0 && (
        <ul data-testid="click-risk-full" className="space-y-0.5 text-[12px] text-text">
          {riskLinesOf(view).map((line, i) => <li key={i}><Inline>{line}</Inline></li>)}
        </ul>
      )}
      <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-[13px]">
        <dt className="text-text-muted">Action</dt>
        <dd>
          <Raw>{view.op}</Raw>
        </dd>
        <dt className="text-text-muted">Workspace</dt>
        <dd data-testid="click-workspace" className="flex flex-wrap items-center gap-x-2">
          <span className="rounded bg-surface-3 px-1 font-mono text-text" data-ws-id>
            id <Raw>{view.workspace.id}</Raw>
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
        <dt className="text-text-muted">Signing key</dt>
        <dd data-testid="click-custody">{view.auth === 'convenience' ? 'One click on this computer, no fingerprint/passphrase' : <Raw>{view.auth}</Raw>}</dd>
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
          The files were checked before signing.
        </p>
      )}
    </>
  )
}

function Presented({ view, digest, problem, signing, onSign, onCancel, waitUntil, ws, story, label: given, extra, ready = true, manyWorkspaces = false, pressed = false, onDisarm }: Omit<Props, 'answer'> & { view: ClickViewAnswer['view']; digest: string; waitUntil: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (waitUntil <= Date.now()) return
    const t = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(t)
  }, [waitUntil])
  const waiting = waitUntil > now
  const picturesOk = usePicturesOk(view.content?.images) // R3 part B: a picture that is not the pasted file switches Sign off
  const risk = riskLinesOf(view)
  const allowed = !signing && !waiting && picturesOk && ready
  const auto = pressed && contractViewOk(view) // the one-press arm: a contract-4 View of a request prepared by a press here
  const press = usePressSign({ key: digest, riskLines: risk, pressed: auto, can: allowed, onSign, onCancel: onDisarm ?? onCancel })
  const arming = press.mode === 'arm' && !signing
  const can = press.mode === 'press' ? press.ready && allowed : (press.mode === 'arm' || press.mode === 'held') && !signing
  // "i" (the Technical details) opened during the arm stops it; a press inside it starts a new one.
  const box = useRef<HTMLElement | null>(null)
  const { root, info } = press
  const setBox = useCallback((el: HTMLElement | null) => {
    box.current = el
    root(el)
  }, [root])
  useEffect(() => {
    const el = box.current
    if (!el) return
    const opened = (e: Event) => {
      if (e.target instanceof HTMLDetailsElement && e.target.open) info(true)
    }
    el.addEventListener('toggle', opened, true)
    return () => el.removeEventListener('toggle', opened, true)
  }, [info])
  const label = story?.signLabel ?? given ?? 'Sign'
  const again = press.mode === 'held' ? <Button type="button" size="sm" data-testid="sign-again" {...press.button}>{label}</Button> : null
  const technical = <TechnicalFacts view={view} action={again} />
  // A compact review (a summary, or a quick task shown folded) says what happens in its own lines: no step list under it.
  const lean = !!view.content?.summary || (!!story?.quick && !!view.content && alsoSigned(view.content, story.quick) !== null)
  const danger = !!story?.danger
  return (
    <section ref={setBox} data-testid="click-panel" aria-label="Review before signing" className="space-y-3">
      <WorkspaceLine workspace={view.workspace} many={manyWorkspaces} always={auto || risk.length > 0} folded={!!story?.foldWorkspace && !manyWorkspaces} target={view.content?.ticket?.key} />
      {view.content && <SignContent content={view.content} ws={view.workspace?.id} technical={technical} compact={story?.compact} quick={story?.quick} />}
      {/* authority stays in the default view, never folded: the scope, the agent access length, what happens next */}
      {story?.scope && <p data-testid="sign-scope" className="text-[13px] text-text">{story.scope}</p>}
      <AgentAccess story={story ?? null} />
      {story && !lean && <WhatNext story={story} />}
      {view.checks.filter(c => !c.clean).map((c, i) => <p key={i} role="note" className="text-[13px] text-warning">Repository <Raw>{c.repo}</Raw>: not clean</p>)}
      {view.artifacts === 'unseen' && <p data-testid="click-artifacts" role="note" className="text-[13px] text-warning">The files could not be checked before signing.</p>}
      {!view.content && <SignDetails>{technical}</SignDetails>}
      {!view.content && <div data-testid="click-lines" className="max-h-64 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px]">{view.lines.map((line, i) => <div key={i} className="whitespace-pre-wrap break-all">{line || " "}</div>)}</div>}
      {problem && <ClickProblemBox problem={{ ...problem, body: problem.body.replace('Press the button again', `Press ${label} again`), next: undefined, signedAt: undefined }} ws={ws} />}
      {extra}
      <SignFooter><div className={danger ? 'shrink-0 border-t border-border pt-3' : STICKY_FOOTER} data-testid="sign-footer" {...{ [ARM_INSIDE]: '' }}>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {press.mode === 'press' && !press.ready && !signing && (
            <span data-testid="sign-arming" role="status" className="mr-auto text-[12px] text-text-muted">
              Ready in a moment
            </span>
          )}
          {arming && (
            <span data-testid="sign-arm-status" role="status" className="mr-auto text-[12px] text-text-muted">
              Signs in a moment. Press again or Esc to stop.
            </span>
          )}
          <Button type="button" variant="ghost" onClick={onCancel} disabled={signing}>
            Cancel
          </Button>
          <Button
            type="button"
            variant={danger ? 'destructive' : 'default'}
            disabled={!can}
            aria-busy={signing}
            data-testid="sign-button"
            className={arming ? 'relative overflow-hidden' : undefined}
            {...press.button}
            title={press.mode !== 'press' || press.ready ? undefined : `${label} turns on after the review has been on screen for a moment`}
          >
            {signing ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden /> Signing…
              </>
            ) : arming ? (
              <>
                <ArmFill key={press.round} />
                <span className="relative"><SignerHeadline view={view} fallback={label} /></span>
              </>
            ) : (
              label
            )}
          </Button>
        </div>
        {/* C3: the signer's risk lines, under the button and never folded; the second press counts only once they are on screen */}
        <RiskLines lines={risk} box={press.riskBox} danger={danger} />
      </div></SignFooter>
    </section>
  )
}
