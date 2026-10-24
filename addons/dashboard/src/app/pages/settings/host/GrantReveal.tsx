// #1198 C7: agent access for a tool outside the dashboard. The page makes a one-use key, sends its public half when the
// request is prepared (so the review the person approves is of that key), and after the click opens the sealed secret
// the signer returns. The secret is shown once, with Copy, then wiped; the dashboard server only ever relayed ciphertext.
import { Check, Copy, KeyRound, Loader2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { revealViewOk, type AgentAccessAction } from '@/api/click'
import { newRevealKey, SealedError, type RevealKey } from '@/api/sealed'
import { accessLength } from '@/api/grants'
import { ClickSignPanel } from '@/components/sign/ClickSignPanel'
import { useClickSign } from '@/components/sign/useClickSign'
import { storyOf } from '@/components/sign/story'
import { Button } from '@/components/ui/button'
import { Raw } from '@/components/sign/visible'

/** How long a shown secret stays on screen before the page wipes it by itself. */
export const SHOW_MS = 5 * 60_000

export interface GrantRevealProps {
  ws: string
  hours: number
  scope: 'workable' | 'all'
  label?: string
  onClose: () => void
  /** The grant list changed (a grant was made). */
  onChanged?: () => void
}

type Shown = { kind: 'secret'; value: string } | { kind: 'lost' } | { kind: 'wiped' } | { kind: 'failed'; reason: string }

export function GrantReveal({ ws, hours, scope, label, onClose, onChanged }: GrantRevealProps) {
  const click = useClickSign(ws)
  const key = useRef<RevealKey | null>(null)
  const publicKey = useRef<string>('')
  const started = useRef(false)
  const [checked, setChecked] = useState<{ digest: string; ok: boolean } | null>(null)
  const [shown, setShown] = useState<Shown | null>(null)
  const [copied, setCopied] = useState(false)
  const action = (reveal_key: string): AgentAccessAction => ({ action: 'agent_access', hours, scope, ...(label ? { label } : {}), reveal_key })

  // One key per request: made here, its public half goes out with prepare.
  useEffect(() => {
    if (started.current) return
    started.current = true
    void (async () => {
      const k = await newRevealKey()
      key.current = k
      publicKey.current = k.publicKeyB64
      await click.start(action(k.publicKeyB64))
    })()
    return () => {
      key.current?.close()
      key.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The request id exists: bind the key to this workspace and request (open() builds its context from these, never from the answer).
  const rid = click.prepared?.request_id
  useEffect(() => {
    if (!rid || !key.current) return
    try { key.current.bind({ workspace_id: ws, request_id: rid }) } catch { /* bound already (a re-render) */ }
  }, [rid, ws])

  // Only a View that is for THIS key, says the secret is shown once here, and carries the risk line gets a Sign button.
  useEffect(() => {
    const v = click.view
    if (!v || !publicKey.current) return
    let stale = false
    void revealViewOk(v.view, ws, publicKey.current).then((ok) => { if (!stale) setChecked({ digest: v.view_digest, ok }) })
    return () => { stale = true }
  }, [click.view, ws])
  const viewOk = click.view && checked?.digest === click.view.view_digest ? checked.ok : null

  // Signed: open the sealed secret once.
  useEffect(() => {
    if (click.phase !== 'done' || !click.signed || !rid || !click.view || shown) return
    onChanged?.()
    const env = click.signed.reveal?.envelope
    if (!env || !key.current) { setShown({ kind: 'lost' }); return }
    const k = key.current
    void (async () => {
      let bytes: Uint8Array | null = null
      try {
        bytes = await k.open(env, { purpose: 'grant.secret', workspace_id: ws, request_id: rid, view_digest: click.view!.view_digest })
        setShown({ kind: 'secret', value: new TextDecoder().decode(bytes) })
      } catch (e) {
        setShown({ kind: 'failed', reason: e instanceof SealedError ? e.reason : 'unknown' })
      } finally {
        bytes?.fill(0)
        k.close() // one reveal per key
      }
    })()
  }, [click.phase, click.signed, click.view, rid, ws, shown, onChanged])

  // Wipe on a timer; the unmount drops everything with the component state.
  useEffect(() => {
    if (shown?.kind !== 'secret') return
    const t = setTimeout(() => setShown({ kind: 'wiped' }), SHOW_MS)
    return () => clearTimeout(t)
  }, [shown])

  const copy = async () => {
    if (shown?.kind !== 'secret') return
    try { await navigator.clipboard.writeText(shown.value); setCopied(true) } catch { setCopied(false) }
  }
  const finish = () => { setShown(null); setCopied(false); key.current?.close(); onClose() }
  const cancel = () => void click.cancel().then(() => { click.reset(true); onClose() })

  if (shown?.kind === 'secret') {
    return (
      <section aria-label="Agent access secret" data-testid="grant-reveal" className="space-y-3">
        <p className="flex items-center gap-2 text-[14px] font-medium"><KeyRound className="size-4" aria-hidden />Access created for {accessLength(hours)}</p>
        <p role="note" data-testid="grant-reveal-once" className="rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-[13px]">
          This secret is shown only once. Copy it now and give it to the tool; orch cannot show it again. If you lose it, stop this access and make a new one.
        </p>
        <code data-testid="grant-secret" className="block max-h-40 overflow-auto break-all rounded-md border border-border bg-bg p-3 font-mono text-[12px] select-all">{shown.value}</code>
        <p className="text-[12px] text-text-muted">The tool reads it as <code>ORCH_GRANT</code>.</p>
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="outline" onClick={() => void copy()}>{copied ? <><Check className="size-4" aria-hidden />Copied</> : <><Copy className="size-4" aria-hidden />Copy</>}</Button>
          <Button onClick={finish}>Done, hide it</Button>
        </div>
      </section>
    )
  }
  if (shown) {
    const text = shown.kind === 'wiped' ? 'The secret was hidden. It cannot be shown again: stop this access in the list and make a new one if you did not copy it.'
      : shown.kind === 'lost' ? 'The access was created, but its secret did not arrive. It cannot be shown again: stop this access in the list and make a new one.'
        : `The access was created, but its secret could not be opened here (${shown.reason}). Stop this access in the list and make a new one.`
    return (
      <section data-testid="grant-reveal-lost" className="space-y-3">
        <p role="alert" className="text-[13px] text-danger">{text}</p>
        <div className="flex justify-end"><Button onClick={finish}>Close</Button></div>
      </section>
    )
  }
  const view = click.view
  return (
    <section aria-label="Agent access for a tool" data-testid="grant-reveal-flow" className="space-y-3">
      {(click.phase === 'idle' || click.phase === 'preparing' || click.phase === 'viewing') && <p className="flex items-center gap-2 text-[13px] text-text-muted"><Loader2 className="size-4 animate-spin" aria-hidden />Preparing the review…</p>}
      {view && (click.phase === 'ready' || click.phase === 'signing') && (viewOk === null ? null : viewOk ? (
        <ClickSignPanel ws={ws} story={storyOf(action(publicKey.current))} answer={view} problem={click.problem} signing={click.phase === 'signing'} onSign={() => void click.sign(publicKey.current)} onCancel={cancel} />
      ) : (
        <p role="alert" data-testid="grant-reveal-bad-view" className="text-[13px] text-danger">The review does not say that the secret is shown here once, for this page. Nothing was signed. <Button size="sm" variant="ghost" onClick={cancel}>Cancel</Button></p>
      ))}
      {click.phase === 'settling' && <p role="status" className="text-[13px] text-text-muted">{click.settleNote ?? 'The signature is settling…'} If the access was created, its secret cannot be shown: stop it in the list and make a new one.</p>}
      {(click.phase === 'failed' || click.phase === 'cancelled') && click.problem && (
        <div className="space-y-2"><p role="alert" className="text-[13px] text-danger"><Raw>{click.problem.title}</Raw>: <Raw>{click.problem.body}</Raw></p><div className="flex justify-end"><Button variant="outline" onClick={onClose}>Close</Button></div></div>
      )}
    </section>
  )
}
