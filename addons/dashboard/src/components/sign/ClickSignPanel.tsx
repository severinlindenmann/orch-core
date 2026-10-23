import { usePicturesOk } from './Pictures'
import { SignContent, SignDetails } from './SignContent'
// The click-to-sign panel (host contract section 0.8.6 item 7): it shows ONLY the signer's View, nothing of this page's
// own preview, the host's fingerprint or a cached ticket. `lines` are core's terminal
// review, already escaped, and are text here, never HTML. Sign is armed after 1 s of visible presentation.
// A word op (grant, policy, members) is confirmed by typing the View's word next to Sign (Sign stays off until it matches); every
// other op signs on one click. The risk notice lives in Settings; the custody is a technical fact under Technical details.
import { Loader2, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { CLICK_RISK, type ClickProblem, type ClickViewAnswer } from '@/api/click'
import { uuid8 } from '@/api/workspaces'
import { Button } from '@/components/ui/button'
import { Raw } from './visible'
import { ClickProblemBox } from './RefusalDevices'
import { useActivation, useArming } from './useArming'
import { TypedWord, WhatNext } from './SignAside'
import type { SignStory } from './story'
import { fmtClock } from '@/lib/time'

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
  onSign: (word?: string) => void
  onCancel: () => void
  /** Rate limit: the time (ms since epoch) before which Sign stays off. */
  waitUntil?: number
  /** The dialog's own workspace: the devices a refusal names are looked up there, never in the View's (review I2). */
  ws?: string
  /** What this signature leads to (button label, steps, access): derived from the action. */
  story?: SignStory | null
}

export function ClickSignPanel({ answer, problem, signing, onSign, onCancel, waitUntil = 0, ws, story }: Props) {
  const { view, view_digest: digest } = answer
  // A new View (a new digest) is a new confirmation: the timer, a half-done hold and any half-begun press start over.
  return <Presented key={digest} view={view} digest={digest} problem={problem} signing={signing} onSign={onSign} onCancel={onCancel} waitUntil={waitUntil} ws={ws} story={story} />
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
          The signer checked the artifact bytes it signs.
        </p>
      )}
    </>
  )
}

function Presented({ view, digest, problem, signing, onSign, onCancel, waitUntil, ws, story }: Omit<Props, 'answer'> & { view: ClickViewAnswer['view']; digest: string; waitUntil: number }) {
  const armed = useArming(digest)
  const [now, setNow] = useState(() => Date.now())
  const [typed, setTyped] = useState('')
  useEffect(() => {
    if (waitUntil <= Date.now()) return
    const t = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(t)
  }, [waitUntil])
  const waiting = waitUntil > now
  const word = view.confirm.kind === 'word' ? view.confirm.word : null
  const wordOk = word === null || typed === word
  const activation = useActivation(armed && wordOk) // a press that began before the word was typed never counts
  const picturesOk = usePicturesOk(view.content?.images) // R3 part B: a picture that is not the pasted file switches Sign off
  const can = armed && !signing && !waiting && wordOk && picturesOk
  const go = () => {
    if (!activation.take() || !can) return
    onSign(word ?? undefined)
  }
  const technical = <TechnicalFacts view={view} />
  const label = story?.signLabel ?? 'Sign'
  return (
    <section data-testid="click-panel" aria-label="Review before signing" className="space-y-3">
      {view.content && <SignContent content={view.content} ws={view.workspace?.id} technical={technical} compact={story?.compact} />}
      {story?.scope && <p data-testid="sign-scope" className="text-[13px] text-text">{story.scope}</p>}
      {word !== null && <TypedWord word={word} story={story ?? null} value={typed} onChange={setTyped} disabled={signing} />}
      {story && <WhatNext story={story} />}
      {view.checks.filter(c => !c.clean).map((c, i) => <p key={i} role="note" className="text-[13px] text-warning">Repository <Raw>{c.repo}</Raw>: not clean</p>)}
      {view.artifacts === 'unseen' && <p data-testid="click-artifacts" role="note" className="text-[13px] text-warning">The signer did not see the artifact bytes: only their hash is signed.</p>}
      {!view.content && <SignDetails>{technical}</SignDetails>}
      {!view.content && <div data-testid="click-lines" className="max-h-64 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px]">{view.lines.map((line, i) => <div key={i} className="whitespace-pre-wrap break-all">{line || " "}</div>)}</div>}
      {problem && <ClickProblemBox problem={{ ...problem, next: undefined, signedAt: undefined }} ws={ws} />}
      <div className="space-y-2">
        <p data-testid="sign-method" className="text-[12px] text-text-muted">On this computer</p>
        {word !== null && !wordOk && armed && <p data-testid="word-hint" className="text-[12px] text-text-muted">Type {word} above to continue.</p>}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel} disabled={signing}>
            Cancel
          </Button>
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
              label
            )}
          </Button>
        </div>
      </div>
    </section>
  )
}
