// The click-to-sign panel (host contract section 0.8.6 item 7): it shows ONLY the signer's View, nothing of this page's
// own preview, the host's fingerprint or a cached ticket. The risk line comes first. `lines` are core's terminal
// review, already escaped, and are text here, never HTML. Sign is armed after 1 s of visible presentation.
import { Loader2, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { CLICK_RISK, type ClickProblem, type ClickViewAnswer } from '@/api/click'
import { uuid8 } from '@/api/workspaces'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Raw } from './visible'
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
}

export function ClickSignPanel({ answer, problem, signing, onSign, onCancel, waitUntil = 0 }: Props) {
  const { view, view_digest: digest } = answer
  // A new View (a new digest) is a new confirmation: the timer, the typed word and any half-begun press start over.
  return <Presented key={digest} view={view} digest={digest} problem={problem} signing={signing} onSign={onSign} onCancel={onCancel} waitUntil={waitUntil} />
}

function Presented({ view, digest, problem, signing, onSign, onCancel, waitUntil }: Omit<Props, 'answer'> & { view: ClickViewAnswer['view']; digest: string; waitUntil: number }) {
  const armed = useArming(digest)
  const activation = useActivation(armed)
  const [word, setWord] = useState('')
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (waitUntil <= Date.now()) return
    const t = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(t)
  }, [waitUntil])
  const waiting = waitUntil > now
  const needsWord = view.confirm.kind === 'word'
  const can = armed && !signing && !waiting && (!needsWord || word.length > 0)
  const go = () => {
    if (!activation.take() || !can) return
    onSign(needsWord ? word : undefined)
  }
  return (
    <section data-testid="click-panel" aria-label="Review before signing" className="space-y-3">
      <RiskLine text={view.risk} />
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
        <dt className="text-text-muted">Signed with</dt>
        <dd data-testid="click-custody">
          {view.auth === 'convenience' ? 'convenience (no presence)' : <Raw>{view.auth}</Raw>}, device <Raw>{view.device}</Raw>
        </dd>
        <dt className="text-text-muted">Expires</dt>
        <dd>
          <When stamp={view.expires_at} />
        </dd>
      </dl>
      <div data-testid="click-lines" className="max-h-64 overflow-auto rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-5 text-text">
        {view.lines.map((line, i) => (
          <div key={i} className="whitespace-pre-wrap break-all">
            {line === '' ? ' ' : line}
          </div>
        ))}
      </div>
      {view.checks.length > 0 && (
        <ul data-testid="click-checks" className="space-y-0.5 text-[12px] text-text-muted">
          {view.checks.map((c, i) => (
            <li key={i}>
              Repository <Raw>{c.repo}</Raw>: {c.clean ? 'clean' : 'not clean'}, checked by <Raw>{c.attested_by}</Raw> at <When stamp={c.at} />
            </li>
          ))}
        </ul>
      )}
      {view.artifacts && (
        <p data-testid="click-artifacts" className="text-[12px] text-text-muted">
          {view.artifacts === 'checked' ? 'The signer checked the artifact bytes it signs.' : 'The signer did not see the artifact bytes: only their hash is signed.'}
        </p>
      )}
      {view.confirm.kind === 'word' && (
        <div className="space-y-1.5">
          <Label htmlFor="click-word">
            To confirm, type <Raw>{view.confirm.word}</Raw>
          </Label>
          <Input
            id="click-word"
            value={word}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setWord(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== 'Enter' || e.repeat) return
              e.preventDefault()
              activation.begin(e)
              go()
            }}
          />
        </div>
      )}
      {problem && (
        <div role="alert" data-testid="click-problem" className="rounded-md border border-warning/40 px-3 py-2 text-[13px]">
          <p className="font-medium text-text">{problem.title}</p>
          <p className="text-text-muted">{problem.body}</p>
        </div>
      )}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <div className="flex items-center gap-2">
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
              'Sign'
            )}
          </Button>
        </div>
      </div>
    </section>
  )
}
