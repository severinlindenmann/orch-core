import type { Workspace } from '@/api/types'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useRouter } from '@tanstack/react-router'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { queries } from '@/api/queries'
import type { HandoffAction, HandoffRequest, HandoffState } from '@/api/handoff'
import { clickEligible, clickReady } from '@/api/click'
import { hubOnly, hubRequestOf, hubSettingsSplit, isAddonGrant } from '@/api/hubsign'
import { useWorkspace } from '@/app/workspace'
import { useMe } from '@/app/useMe'
import { Button } from '@/components/ui/button'
import { lastTrigger, SignShell } from './SignShell'
import { HubAddonPanel, HubSettingsPanel, HubSignPanel, NotFromHub } from './HubSignPanel'
import { Inline } from './visible'
import { ClickSignPanel } from './ClickSignPanel'
import { ClickProblemBox } from './RefusalDevices'
import { useClickSign, type ClickPhase } from './useClickSign'
import { storyOf } from './story'
import { takeTrustedPress, trustedPress } from './trustedPress'
export interface SignPopoverProps {
  /** Core's title: "Approve plan", "Comment", "Close DEMO-0043". */
  title: string
  workspace?: Workspace
  ws: string | undefined
  /** What to prepare. Null while the form is not complete (the button stays off). */
  action: HandoffAction | null
  /** The page starts an agent after this signature (with access already there or given by it): the steps say so. */
  startsAgent?: boolean
  /** Button that starts the preparation when there is a form: the verb, in core's words. */
  confirmLabel?: string
  /** Why the button is off, shown beside it. */
  hint?: string | null
  /** The form, before the request exists. */
  children?: ReactNode
  /** Choices that are part of what is signed (agent access length): shown above the review in every state but signing. A change of `action` needs a new key on this component, which prepares again. */
  controls?: ReactNode
  /** A plain-words summary above the review, in every state but signing. Unlike `controls` it never holds back the preparation. */
  lead?: ReactNode
  /** Exact action summary, kept visible while preparing and reviewing. */
  summary?: ReactNode
  /** Said above the panel while it is open: a warning that the page changed under it. */
  warning?: (request: HandoffRequest) => string | null
  /** Said under the review and outside the signed content: what happens next that this signature does NOT cover. */
  unsigned?: ReactNode
  /** The signer's event is in the log. The review closes by itself right after. */
  onSettled?: (phase: ClickPhase, state: HandoffState | null, request: HandoffRequest | null) => void
  onPending?: (pending: boolean) => void
  /** True while the signature itself is being made. With this set the review folds into the anchor button, which says "Signing…". */
  onSigning?: (signing: boolean) => void
  /** FB15: the host said `sign.hub_only` although this page's workspace card was stale: show the hub panel. */
  forceHub?: boolean
  /** A click signature was rejected or cancelled. */
  onInterrupted?: () => void
  /** The ONE toast after a signature (default: "<title>: signed"). An empty string: the caller says the outcome itself once its follow-up step is done. */
  doneToast?: string
  /** The button this review belongs to. Default: the button that was pressed last. */
  anchor?: HTMLElement | null
  onClose: () => void
  /** Show the review in place, under the row that asked (an accordion), not as a popover. */
  inline?: boolean
  initialFocus?: () => HTMLElement | null
}

export function SignPopover(p: SignPopoverProps) {
  const [anchor] = useState(() => p.anchor !== undefined ? p.anchor : lastTrigger())
  const cancelRef = useRef<HTMLButtonElement>(null)
  const { workspaces } = useWorkspace()
  const known = useQuery(queries.workspaces())
  const here = p.workspace ?? workspaces.find(w => w.id === p.ws) ?? known.data?.find(w => w.id === p.ws)
  const [hubRefused, setHubRefused] = useState(false)
  const viaHub = hubOnly(here) || p.forceHub === true || hubRefused
  const click = useClickSign(p.ws, (_phase, state, request) => {
    if (p.doneToast !== '') toast.success(p.doneToast ?? `${p.title}: signed`)
    p.onSettled?.('done', state, request)
  })
  const callbacks = useRef(p)
  callbacks.current = p
  const qc = useQueryClient()
  const router = useRouter({ warn: false })
  const me = useMe(p.ws).data
  const story = storyOf(p.action, { startsAgent: p.startsAgent, person: me?.person })
  const canClick = clickReady(here) && (!p.action || clickEligible(p.action))
  const started = useRef(false)
  useLayoutEffect(() => {
    if (!viaHub && canClick && p.action && !p.children && !p.controls && !p.hint && !started.current && takeTrustedPress()) {
      started.current = true
      void click.start(p.action)
    }
  })
  const refusedByHost = (click.error as { code?: string } | null)?.code === 'sign.hub_only'
  useEffect(() => { if (refusedByHost) { setHubRefused(true); void qc.invalidateQueries({ queryKey: ['workspaces'] }) } }, [refusedByHost, qc])
  useEffect(() => {
    if (click.phase === 'cancelled' || (click.phase === 'failed' && ['prepare', 'retry', 'again', 'review'].includes(click.problem?.next ?? ''))) p.onInterrupted?.()
  }, [click.phase, click.problem, p.onInterrupted])
  useEffect(() => { callbacks.current.onPending?.(['preparing', 'viewing', 'ready', 'signing', 'settling'].includes(click.phase)) }, [click.phase])
  useEffect(() => { callbacks.current.onSigning?.(click.phase === 'signing') }, [click.phase])
  useEffect(() => () => callbacks.current.onSigning?.(false), [])
  useEffect(() => { if (click.phase === 'done' || click.switched || click.leftUnsettled) p.onClose() }, [click.phase, click.switched, click.leftUnsettled, p.onClose])
  const reported = useRef<string | null>(null)
  useEffect(() => {
    const prepared = click.prepared
    if (click.phase !== 'failed' || click.problem?.next !== 'open' || !prepared || !p.ws || reported.current === prepared.request_id) return
    reported.current = prepared.request_id
    void api.stateClick(p.ws, prepared.request_id).then(got => {
      if (!got.key || !got.progress) return
      p.onSettled?.('failed', { id: prepared.request_id, state: 'submitted', op: prepared.op, key: got.key, expires_at: prepared.expires_at, event_ids: [], seq: null, progress: got.progress }, null)
    }, () => {})
  }, [click.phase, click.problem, click.prepared, p.ws, p.onSettled])
  const [waitUntil, setWaitUntil] = useState(0)
  useEffect(() => { if (click.problem?.waitMs) setWaitUntil(Date.now() + click.problem.waitMs) }, [click.problem])
  const close = async () => {
    if (click.prepared && click.problem?.next !== 'check') await click.cancel()
    p.onClose()
  }
  if (viaHub) {
    const request = hubRequestOf(p.action)
    return (
      <SignShell inline={p.inline} anchor={anchor} open onDismiss={p.onClose} title={<Inline>{p.title}</Inline>} description={null} showClose onOpenAutoFocus={(e) => e.preventDefault()}>
        {isAddonGrant(p.action) && p.ws ? <HubAddonPanel ws={p.ws} action={p.action} onDone={() => p.onSettled?.('done', null, null)} /> : hubSettingsSplit(p.action) && p.ws ? <HubSettingsPanel ws={p.ws} action={p.action as Extract<HandoffAction, { action: 'settings_set' }>} onDone={() => p.onSettled?.('done', null, null)} /> : request && p.ws ? <HubSignPanel ws={p.ws} request={request} /> : <NotFromHub action={p.action?.action} />}
        <div className="flex justify-end gap-2">
          <Button variant="outline" ref={cancelRef} onClick={p.onClose}>
            Close
          </Button>
        </div>
      </SignShell>
    )
  }

  return <SignShell inline={p.inline} anchor={anchor} open={!(p.onSigning && click.phase === 'signing')} onDismiss={() => void close()} title={<Inline>{click.view?.view.content?.title ?? click.view?.view.headline ?? p.title}</Inline>} showClose onOpenAutoFocus={e => { e.preventDefault(); (p.initialFocus?.() ?? cancelRef.current)?.focus() }}>
    {p.controls}{p.lead}{!click.view && p.summary}
    {!canClick ? <><p>One click is not ready on this computer.</p>{router && <Link to="/settings/$tab" params={{ tab: 'general' }} onClick={() => void close()}>Turn on One click</Link>}</> : <>
      {click.phase === 'idle' && <>{p.children}{p.hint && <p>{p.hint}</p>}<Button disabled={!p.action || !!p.hint} onClick={e => { if (trustedPress(e.nativeEvent) && p.action) void click.start(p.action) }}>{p.confirmLabel ?? story?.signLabel ?? p.title}</Button></>}
      {['preparing', 'viewing'].includes(click.phase) && <p role="status">Reading the review…</p>}
      {(click.phase === 'ready' || click.phase === 'signing') && click.view && <ClickSignPanel ws={p.ws} answer={click.view} problem={click.problem} signing={click.phase === 'signing'} onSign={() => void click.sign()} onCancel={() => void close()} waitUntil={waitUntil} story={story} label={p.confirmLabel} />}
      {click.phase === 'settling' && <p role="status">The outcome is not certain yet. Do not sign again. {click.settleNote}</p>}
      {click.phase === 'failed' && click.problem && (click.problem.next === 'unavailable' ? <p>{click.problem.body}</p> : <ClickProblemBox problem={click.problem} ws={p.ws} />)}
      {click.phase === 'cancelled' && <p role="status">Nothing was signed.</p>}
      {(click.phase === 'cancelled' || (click.phase === 'failed' && ['prepare', 'retry', 'again', 'review'].includes(click.problem?.next ?? ''))) && <Button onClick={e => { if (trustedPress(e.nativeEvent) && p.action) void click.again(p.action) }}>Try again</Button>}
      {p.unsigned}
    </>}
  </SignShell>
}
