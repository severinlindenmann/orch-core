// Dashboard execution on or off by a plain click (#1198 C14). The review is core's own (the signing helper builds it
// and the server checks it); the button below it acts only on that review's digest. Turning it on is allowed from a
// dashboard opened on this computer for now (the server says so otherwise); turning it off works everywhere.
import { useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { toast } from 'sonner'
import { agentsApi, RUNNER_LINE, type RunnerReview } from '@/api/agents'
import { ApiError } from '@/api/types'
import { requestReverse } from '@/components/sign/ReverseUndoHost'
import { useActivation, useArming } from '@/components/sign/useArming'
import { Prose } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'

const said = (e: unknown) => (e instanceof ApiError ? e.message : 'The dashboard server did not answer.')

export function RunnerSwitch({ ws, on, initiallyOpen = false, onClosed, terminals: withTerminals = true, initialTerminals = false, label, testId = 'runner-switch', variant = 'default' }: {
  ws: string
  initiallyOpen?: boolean
  onClosed?: () => void
  /** What the button does: turn dashboard execution on (true) or off (false). */
  on: boolean
  /** On only: offer the choice to keep terminals off (it is the CLI's `--no-terminals`). */
  terminals?: boolean
  /** On only: the terminals box starts ticked (places whose whole purpose is terminals). Default: unticked. */
  initialTerminals?: boolean
  /** The button's words when they differ from the default. */
  label?: string
  testId?: string
  variant?: 'default' | 'secondary'
}) {
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [terminals, setTerminals] = useState(initialTerminals)
  const [review, setReview] = useState<RunnerReview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const armed = useArming(review?.view_digest ?? 'no-review')
  const activation = useActivation(armed && !!review && !busy)
  const errorId = useId()
  const seq = useRef(0)

  const read = async (t: boolean) => {
    const mine = ++seq.current
    setReview(null)
    setError(null)
    try {
      const got = await agentsApi.runnerReview(ws, { on, terminals: t })
      if (seq.current !== mine) return
      if (on && (!got.view.lines.includes(RUNNER_LINE) || got.view.terminals !== t)) throw new ApiError(409, { code: 'human.click_refused', message: 'The review is not what it should be. Nothing changed.', retryable: false })
      setReview(got)
    } catch (e) {
      if (seq.current === mine) setError(said(e))
    }
  }
  useEffect(() => () => { seq.current++ }, [])

  const start = () => { setOpen(true); setTerminals(initialTerminals); void read(initialTerminals) }
  const close = () => { seq.current++; setOpen(false); setReview(null); setError(null); onClosed?.() }
  useEffect(() => { if (initiallyOpen) start() }, []) // a fresh review for Undo

  const confirm = async () => {
    if (!review || busy) return
    setBusy(true)
    setError(null)
    try {
      await agentsApi.runnerSwitch(ws, { on, terminals, view_digest: review.view_digest })
      toast.success(on ? 'Dashboard execution is on' : 'Dashboard execution is off', on ? {} : { action: { label: 'Undo', onClick: () => requestReverse({ ws, runner: true }) } })
      close()
      await qc.invalidateQueries()
    } catch (e) {
      setError(said(e))
    } finally {
      setBusy(false)
    }
  }

  if (!open) return <Button size="sm" variant={variant} data-testid={testId} onClick={start}>{label ?? (on ? 'Turn on dashboard execution' : 'Turn off dashboard execution')}</Button>
  return (
    <div data-testid={`${testId}-review`} className="w-full space-y-2 rounded-md border border-border bg-surface p-3 text-[13px]">
      {review ? (
        <ul className="space-y-1.5" aria-label={on ? 'What turning it on allows' : 'What turning it off does'}>
          {review.view.lines.map((line, i) => <li key={i} className={i === 0 ? 'font-medium text-text' : 'text-text-muted'}><Prose inline>{line}</Prose></li>)}
        </ul>
      ) : !error ? <p role="status" className="text-text-muted">Reading what this changes…</p> : null}
      {on && withTerminals && (
        <label className="flex items-center gap-2 text-text-muted">
          <input type="checkbox" data-testid={`${testId}-terminals`} checked={terminals} disabled={busy} onChange={(e) => { setTerminals(e.target.checked); void read(e.target.checked) }} />
          Also allow terminals in the dashboard (an extra: a shell that runs as you)
        </label>
      )}
      {error && <p role="alert" id={errorId} data-testid={`${testId}-error`} className="text-warning">{error}</p>}
      <div className="flex flex-wrap justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
        <Button size="sm" data-testid={`${testId}-confirm`} disabled={!review || busy || !armed} onPointerDown={e => activation.begin(e.nativeEvent)} onKeyDown={e => activation.begin(e.nativeEvent)} onClick={e => { if (activation.take(e.nativeEvent)) void confirm() }} aria-describedby={error ? errorId : undefined}>
          {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null} {on ? 'Turn on' : 'Turn off'}
        </Button>
      </div>
    </div>
  )
}
