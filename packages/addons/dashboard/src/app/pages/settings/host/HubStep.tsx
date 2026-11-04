// #1198 C10: a hub step (join, leave) signed with one click, with the person's backup key typed once in this page and
// sealed to the signer (purpose hub.backup_key). The dashboard server only relays ciphertext. The typed words live in
// the input element only: they are read once at the click, the field is emptied at once, and nothing else holds them
// (no state, no storage). Core refuses the step unless the key is the person's own.
import { KeyRound, Loader2 } from 'lucide-react'
import { InfoDrawer } from '@/components/InfoDrawer'
import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import { useWorkspace } from '@/app/workspace'
import { backupViewOk, plainHubViewOk, type HubJoinAction, type HubLeaveAction, type HubModeFullAction, type RemoteDeviceConfirmAction, type RemoteSetAction } from '@/api/click'
import { ApiError } from '@/api/types'
import { seal } from '@/api/sealed'
import { ClickSignPanel } from '@/components/sign/ClickSignPanel'
import { storyOf } from '@/components/sign/story'
import { useClickSign } from '@/components/sign/useClickSign'
import { Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'

export interface HubStepProps {
  ws: string
  action: HubJoinAction | HubModeFullAction | HubLeaveAction | RemoteSetAction | RemoteDeviceConfirmAction
  onClose: () => void
  /** The hub state changed (the step was signed). */
  onChanged?: () => void
  /** What "done" says (default: Signed.). */
  onFailedChange?: (failed: boolean) => void
  doneText?: string
}

/** The sign bar sticks to the bottom of the dialog and would cover the help text under the field: scroll the dialog just
 * far enough that the whole field block ends above the bar. Nothing changes for other sign panels. */
function clearOfSignBar(block: HTMLElement | null) {
  const bar = block?.closest('[data-testid="click-panel"]')?.querySelector('[data-testid="sign-footer"]')
  if (!block || !bar) return
  let scroller: HTMLElement | null = block.parentElement
  while (scroller && !(scroller.scrollHeight > scroller.clientHeight && /auto|scroll/.test(getComputedStyle(scroller).overflowY))) scroller = scroller.parentElement
  const over = block.getBoundingClientRect().bottom - bar.getBoundingClientRect().top + 8
  if (scroller && over > 0) scroller.scrollTop += over
}

export function HubStep({ ws, action, onClose, onChanged, onFailedChange, doneText }: HubStepProps) {
  // A join is only the first half: the host keeps the workspace's add-ons paused until the hub confirms, and reading the
  // pages behind the dialog before that is refused (409). JoinFlow reads them once, after "Connected".
  // The review names the workspace for a person with several (same count as SignPopover): union of the context and the list.
  const { workspaces } = useWorkspace()
  const known = useQuery(queries.workspaces())
  const many = new Set([...workspaces, ...(known.data ?? [])].map(w => w.id)).size > 1
  const click = useClickSign(ws, undefined, undefined, { refetchOnDone: action.action !== 'hub_join' })
  const field = useRef<HTMLInputElement>(null)
  const box = useRef<HTMLDivElement>(null)
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

  const failed = click.phase === 'failed' || click.phase === 'cancelled'
  useEffect(() => { onFailedChange?.(failed) }, [failed, onFailedChange])

  const rid = click.prepared?.request_id
  const view = click.view
  const plain = !!view && plainHubViewOk(view.view, ws) // the person key is kept: nothing to type
  const viewOk = view ? plain || backupViewOk(view.view, ws) : null
  const cancel = () => void click.cancel().then(() => { click.reset(true); onClose() })

  const sign = async () => {
    const el = field.current
    if (plain) { await click.sign(); return }
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

  // Fold exact values for presentation; the hook keeps the original View and digest for signing.
  const presented = view && {
    ...view,
    view: {
      ...view.view,
      content: {
        ...(view.view.content ?? { headline: '', sections: [{ id: 'hub-review', title: 'Signed review', text: view.view.lines.join('\n') }] }),
        summary: view.view.content?.summary ?? { title: 'Hub review', lines: [] },
      },
    },
  }
  const agentsEnd = action.action === 'hub_mode_full' && view?.view.lines.some(line =>
    /agent access (?:on this computer )?(?:ends|is revoked|will end|will be revoked)/i.test(line),
  )
  const failureText = action.action === 'hub_leave' ? "Couldn't leave the hub. Nothing changed. Try again."
    : action.action === 'hub_mode_full' ? "Couldn't switch. Try again."
      : action.action === 'remote_set' ? "Couldn't turn on remote access. Nothing changed. Try again."
        : action.action === 'remote_device_confirm' ? "Couldn't add the device. Nothing changed. Try again." : "Couldn't connect. Try again."
  const label = action.action === 'hub_leave' ? 'Leave the hub' : action.action === 'hub_mode_full' ? 'Let the hub read everything'
    : action.action === 'remote_set' ? 'Turn on remote access' : action.action === 'remote_device_confirm' ? 'Add a device' : 'Join the hub'

  return (
    <section aria-label={label} data-testid="hub-step" className="space-y-3">
      {(click.phase === 'idle' || click.phase === 'preparing' || click.phase === 'viewing') && (
        <p className="flex items-center gap-2 text-[13px] text-text-muted"><Loader2 className="size-4 animate-spin" aria-hidden />Preparing the review…</p>
      )}
      {view && (click.phase === 'ready' || click.phase === 'signing') && (viewOk ? (
        <>
        {agentsEnd && <p>Agent access on this computer ends.</p>}
        <ClickSignPanel
          ws={ws}
          manyWorkspaces={many}
          story={{ ...storyOf(action)!, steps: [] }}
          answer={presented!}
          problem={click.problem}
          signing={click.phase === 'signing' || sealing}
          ready={plain || filled}
          onSign={() => void sign()}
          onCancel={cancel}
          extra={plain ? undefined :
            <div className="space-y-2" data-testid="hub-backup" ref={box}>
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
                onFocus={() => clearOfSignBar(box.current)}
                onChange={(e) => setFilled(e.currentTarget.value.trim().length > 0)}
                className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-[14px] text-text"
                aria-describedby="hub-backup-help"
              />
              <p id="hub-backup-help" className="text-[12px] text-text-muted">The 24 words you wrote down when you set up orch, in order, separated by spaces. They are sent in a locked form to your signing helper on this computer and are not kept.</p>
              {refused && <p role="alert" className="text-[13px] text-danger">{refused}</p>}
            </div>
          }
        />
        </>
      ) : (
        <p role="alert" data-testid="hub-bad-view" className="text-[13px] text-danger">
          The review does not ask for your backup key the way it should. Nothing was signed. <Button size="sm" variant="ghost" onClick={cancel}>Cancel</Button>
        </p>
      ))}
      {click.phase === 'settling' && <p role="status" className="text-[13px] text-text-muted">{click.settleNote ?? 'The signature is settling…'}</p>}
      {click.phase === 'done' && (
        <div className="space-y-2"><p role="status" data-testid="hub-step-done" className="text-[13px] text-text">{doneText ?? 'Signed.'}</p><div className="flex justify-end"><Button onClick={onClose}>Close</Button></div></div>
      )}
      {failed && (
        <div className="space-y-3">
          <p role="alert" className="text-[13px] text-danger">{failureText}</p>
          <InfoDrawer>
            {click.problem && <p><Raw>{click.problem.title}</Raw>: <Raw>{click.problem.body}</Raw></p>}
            {click.problem?.details && <p><Raw>{click.problem.details}</Raw></p>}
            {click.error instanceof ApiError && <p><Raw>{click.error.code}</Raw>: <Raw>{click.error.message}</Raw></p>}
          </InfoDrawer>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={cancel}>Cancel</Button>
            <Button onClick={() => { setFilled(false); setRefused(null); void click.again(action) }}>Try again</Button>
          </div>
        </div>
      )}
    </section>
  )
}
