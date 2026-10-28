import { usePicturesOk } from './Pictures'
import { SignContent, SignDetails } from './SignContent'
// The click-to-sign panel (host contract section 0.8.6 item 7): it shows ONLY the signer's View, nothing of this page's
// own preview, the host's fingerprint or a cached ticket. `lines` are core's terminal
// review, already escaped, and are text here, never HTML. Sign is armed after 1 s of visible presentation.
// Every op signs on one plain click after the review; nothing is typed. The risk notice lives in Settings; the custody is a technical fact under Technical details.
import { Loader2, ShieldAlert } from 'lucide-react'
import { useEffect, useState, type MouseEvent, type ReactNode } from 'react'
import { CLICK_RISK, type ClickProblem, type ClickViewAnswer } from '@/api/click'
import { Button } from '@/components/ui/button'
import { Raw } from './visible'
import { ClickProblemBox } from './RefusalDevices'
import { useActivation, useArming } from './useArming'
import type { SignStory } from './story'
import { AgentAccess, WhatNext } from './SignAside'
import { alsoSigned } from './alsoSigned'
import { fmtClock } from '@/lib/time'
import { SignFooter, STICKY_FOOTER } from './SignShell'
import { WorkspaceLine } from './WorkspaceLine'

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
}

export function ClickSignPanel({ answer, problem, signing, onSign, onCancel, waitUntil = 0, ws, story, label, extra, ready = true, manyWorkspaces = false }: Props) {
  const { view, view_digest: digest } = answer
  // A new View (a new digest) is a new confirmation: the timer, a half-done hold and any half-begun press start over.
  return <Presented key={digest} view={view} digest={digest} problem={problem} signing={signing} onSign={onSign} onCancel={onCancel} waitUntil={waitUntil} ws={ws} story={story} label={label} extra={extra} ready={ready} manyWorkspaces={manyWorkspaces} />
}

/** What the one "Technical details" expander of the review holds besides the content's own ids (action, workspace, fingerprint, device, the signer's lines and checks). */
export function TechnicalFacts({ view }: { view: ClickViewAnswer['view'] }) {
  return (
    <>
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

function Presented({ view, digest, problem, signing, onSign, onCancel, waitUntil, ws, story, label: given, extra, ready = true, manyWorkspaces = false }: Omit<Props, 'answer'> & { view: ClickViewAnswer['view']; digest: string; waitUntil: number }) {
  const armed = useArming(digest)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (waitUntil <= Date.now()) return
    const t = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(t)
  }, [waitUntil])
  const waiting = waitUntil > now
  const activation = useActivation(armed)
  const picturesOk = usePicturesOk(view.content?.images) // R3 part B: a picture that is not the pasted file switches Sign off
  const can = armed && !signing && !waiting && picturesOk && ready
  const go = (e: MouseEvent) => {
    if (!activation.take(e.nativeEvent) || !can) return // a synthetic click or a key repeat never signs
    onSign()
  }
  const technical = <TechnicalFacts view={view} />
  // A compact review (a summary, or a quick task shown folded) says what happens in its own lines: no step list under it.
  const lean = !!view.content?.summary || (!!story?.quick && !!view.content && alsoSigned(view.content, story.quick) !== null)
  const label = story?.signLabel ?? given ?? 'Sign'
  const danger = !!story?.danger
  return (
    <section data-testid="click-panel" aria-label="Review before signing" className="space-y-3">
      <WorkspaceLine workspace={view.workspace} many={manyWorkspaces} />
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
      <SignFooter><div className={danger ? 'shrink-0 border-t border-border pt-3' : STICKY_FOOTER} data-testid="sign-footer">
        <div className="flex flex-wrap items-center justify-end gap-2">
          {!armed && !signing && (
            <span data-testid="sign-arming" role="status" className="mr-auto text-[12px] text-text-muted">
              Ready in a moment
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
            onPointerDown={activation.begin}
            onKeyDown={activation.begin}
            onClick={go}
            title={armed ? undefined : `${label} turns on after the review has been on screen for a moment`}
          >
            {signing ? (
              <>
                <Loader2 className="size-4 animate-spin" aria-hidden /> Signing…
              </>
            ) : (
              label
            )}
          </Button>
        </div>
      </div></SignFooter>
    </section>
  )
}
