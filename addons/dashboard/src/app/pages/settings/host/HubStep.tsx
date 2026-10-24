// #1198 C10: a hub step (join, leave) signed with one click, with the person's backup key typed once in this page and
// sealed to the signer (purpose hub.backup_key). The dashboard server only relays ciphertext. The typed words live in
// the input element only: they are read once at the click, the field is emptied at once, and nothing else holds them
// (no state, no storage). Core refuses the step unless the key is the person's own.
import { KeyRound, Loader2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { backupViewOk, type HubJoinAction, type HubLeaveAction } from '@/api/click'
import { seal } from '@/api/sealed'
import { ClickSignPanel } from '@/components/sign/ClickSignPanel'
import { storyOf } from '@/components/sign/story'
import { useClickSign } from '@/components/sign/useClickSign'
import { Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'

export interface HubStepProps {
  ws: string
  action: HubJoinAction | HubLeaveAction
  onClose: () => void
  /** The hub state changed (the step was signed). */
  onChanged?: () => void
  /** What "done" says (default: Signed.). */
  doneText?: string
}

export function HubStep({ ws, action, onClose, onChanged, doneText }: HubStepProps) {
  const click = useClickSign(ws)
  const field = useRef<HTMLInputElement>(null)
  const started = useRef(false)
  const [filled, setFilled] = useState(false)
  const [sealing, setSealing] = useState(false)
  const [refused, setRefused] = useState<string | null>(null)

  useEffect(() => {
    if (started.current) return
    started.current = true
    void click.start(action)
    return () => {
      if (field.current) field.current.value = '' // nothing typed outlives the page
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (click.phase === 'done') onChanged?.()
  }, [click.phase, onChanged])

  const rid = click.prepared?.request_id
  const view = click.view
  const viewOk = view ? backupViewOk(view.view, ws) : null
  const cancel = () => void click.cancel().then(() => { click.reset(true); onClose() })

  const sign = async () => {
    const el = field.current
    if (!el || !view || !rid) return
    let text: string | null = el.value
    el.value = '' // cleared before anything else happens: a failure never leaves the key on screen
    setFilled(false)
    setRefused(null)
    setSealing(true)
    try {
      const env = await seal(view.view.seal!.pk!, text.trim().split(/\s+/).join(' '), { purpose: 'hub.backup_key', workspace_id: ws, request_id: rid, view_digest: view.view_digest })
      text = null
      await click.sign(undefined, { 'hub.backup_key': env })
    } catch {
      setRefused('Your backup key could not be prepared in this window. Nothing was signed. Type it again.')
    } finally {
      text = null
      setSealing(false)
    }
  }

  return (
    <section aria-label={action.action === 'hub_leave' ? 'Leave the hub' : 'Join the hub'} data-testid="hub-step" className="space-y-3">
      {(click.phase === 'idle' || click.phase === 'preparing' || click.phase === 'viewing') && (
        <p className="flex items-center gap-2 text-[13px] text-text-muted"><Loader2 className="size-4 animate-spin" aria-hidden />Preparing the review…</p>
      )}
      {view && (click.phase === 'ready' || click.phase === 'signing') && (viewOk ? (
        <ClickSignPanel
          ws={ws}
          story={storyOf(action)}
          answer={view}
          problem={click.problem}
          signing={click.phase === 'signing' || sealing}
          ready={filled}
          onSign={() => void sign()}
          onCancel={cancel}
          extra={
            <div className="space-y-2" data-testid="hub-backup">
              <label className="block text-[13px] font-medium text-text" htmlFor="hub-backup-key">
                <KeyRound className="mr-1 inline size-4" aria-hidden />Backup key (from setup)
              </label>
              <input
                id="hub-backup-key"
                ref={field}
                type="password"
                autoComplete="off"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                data-testid="hub-backup-input"
                onChange={(e) => setFilled(e.currentTarget.value.trim().length > 0)}
                className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[14px] text-text"
                aria-describedby="hub-backup-help"
              />
              <p id="hub-backup-help" className="text-[12px] text-text-muted">The 24 words you wrote down when you set up orch, in order, separated by spaces. They are sent in a locked form to your signing helper on this computer and are not kept.</p>
              {refused && <p role="alert" className="text-[13px] text-danger">{refused}</p>}
            </div>
          }
        />
      ) : (
        <p role="alert" data-testid="hub-bad-view" className="text-[13px] text-danger">
          The review does not ask for your backup key the way it should. Nothing was signed. <Button size="sm" variant="ghost" onClick={cancel}>Cancel</Button>
        </p>
      ))}
      {click.phase === 'settling' && <p role="status" className="text-[13px] text-text-muted">{click.settleNote ?? 'The signature is settling…'}</p>}
      {click.phase === 'done' && (
        <div className="space-y-2"><p role="status" data-testid="hub-step-done" className="text-[13px] text-text">{doneText ?? 'Signed.'}</p><div className="flex justify-end"><Button onClick={onClose}>Close</Button></div></div>
      )}
      {(click.phase === 'failed' || click.phase === 'cancelled') && click.problem && (
        <div className="space-y-2">
          <p role="alert" className="text-[13px] text-danger"><Raw>{click.problem.title}</Raw>: <Raw>{click.problem.body}</Raw></p>
          <div className="flex justify-end"><Button variant="outline" onClick={onClose}>Close</Button></div>
        </div>
      )}
    </section>
  )
}
