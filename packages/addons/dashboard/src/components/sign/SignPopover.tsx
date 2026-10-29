import type { Workspace } from '@/api/types'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useRouter } from '@tanstack/react-router'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { queries } from '@/api/queries'
import type { HandoffAction, HandoffRequest, HandoffState } from '@/api/handoff'
import { clickEligible, clickReady, DEPOSIT_RISK } from '@/api/click'
import { scheduleKey, type ScheduleList } from '@/api/schedules'
import { useWorkspace } from '@/app/workspace'
import { useMe } from '@/app/useMe'
import { Button } from '@/components/ui/button'
import { lastTrigger, SignShell } from './SignShell'
import { Inline } from './visible'
import { ClickSignPanel, SignerHeadline } from './ClickSignPanel'
import { ClickProblemBox, RefusalDevices } from './RefusalDevices'
import { settledAs, useClickSign, type ClickPhase } from './useClickSign'
import { storyOf } from './story'
import { takeTrustedPress, trustedPress } from './trustedPress'
import { HubAddonPanel, HubSettingsPanel } from './HubSignPanel'
import { hubSettingsSplit, isAddonGrant } from '@/api/hubsign'
import { ClaudeConsentLine, isOwner, useClaudeConsent, type ClaudeConsent } from './claudeConsent'

/**
 * How a settled signature was made. `unsignedShown` is true only on the one-click path, when the `unsigned` slot was
 * rendered in this very popover, open and next to the click review the person signed from. The hub and a review where
 * the slot was never on screen (folded, minimized, a reopened instance) say false.
 */
export interface SignedHow {
  via: 'click' | 'hub'
  unsignedShown: boolean
}

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
  /** The signer's event is in the log. The review closes by itself right after. `how`: which path signed, and whether the
   * `unsigned` slot was on screen next to the one-click review of THIS instance before its signature (see `SignedHow`). */
  onSettled?: (phase: ClickPhase, state: HandoffState | null, request: HandoffRequest | null, how?: SignedHow) => void
  onPending?: (pending: boolean) => void
  /** True while the signature itself is being made. With this set the review folds into the anchor button, which says "Signing…". */
  onSigning?: (signing: boolean) => void
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
  /** This signature starts a Claude Code agent: an owner is asked, in one preselected line, to let it open the ticket's folder without asking. */
  claudeCode?: boolean
  /** The caller's own consent state (it showed the line itself or hands it over): this popover then makes none. */
  consent?: ClaudeConsent
  /** The person asked to approve in the hub instead (an optional extra, A1/FB15): show the hub panel, sign nothing here. */
  forceHub?: boolean
  /** The host's minimum scheduling lead in seconds, for a "too soon" refusal. Default: the schedules list's
   * `min_lead_s` when the page has read it, else an hour. */
  minLeadS?: number | null
}

/** The lead in seconds when known (the prop, else the schedules list already read); undefined: the default hour. */
function knownLead(own: number | null | undefined, list: ScheduleList | undefined): number | undefined {
  for (const v of [own, list?.min_lead_s]) if (typeof v === 'number' && v >= 0) return v
  return undefined
}

export function SignPopover(p: SignPopoverProps) {
  const [anchor] = useState(() => p.anchor !== undefined ? p.anchor : lastTrigger())
  const cancelRef = useRef<HTMLButtonElement>(null)
  // The `unsigned` slot was on screen in THIS instance with the click review the person signs from (set below, never reset).
  const unsignedShown = useRef(false)
  const unsignedEl = useRef<HTMLDivElement>(null)
  const hub: SignedHow = { via: 'hub', unsignedShown: false }
  // run 13: a schedule refused as too soon says the host's own lead (the server's min_lead_s), not always "1 hour"
  const qc = useQueryClient()
  const leadS = knownLead(p.minLeadS, p.ws ? qc.getQueryData<ScheduleList>(scheduleKey(p.ws)) : undefined)
  const { workspaces } = useWorkspace()
  const known = useQuery(queries.workspaces())
  const here = p.workspace ?? workspaces.find(w => w.id === p.ws) ?? known.data?.find(w => w.id === p.ws)
  const many = new Set([...workspaces, ...(known.data ?? [])].map(w => w.id)).size > 1
  // A1/D83: local signing is valid in every hub mode, so the review never routes by the workspace's hub mode.
  // The hub is an optional extra path: only when the caller asks for it, and only for what the hub can approve here.
  // An add-on grant goes through the one hub approval route (FB15): the host prepares it for the hub, nothing signs here.
  const viaHub = p.forceHub === true && (isAddonGrant(p.action) || !!hubSettingsSplit(p.action))
  const consentRef = useRef<ReturnType<typeof useClaudeConsent> | null>(null)
  const click = useClickSign(p.ws, (_phase, state, request) => {
    const settle = () => {
      if (p.doneToast !== '') toast.success(p.doneToast ?? `${p.title}: signed`)
      p.onSettled?.('done', state, request, { via: 'click', unsignedShown: unsignedShown.current })
    }
    // P03: the consent is recorded after the signature settled and before the caller starts the agent (a failure never stops the start)
    const recorded = consentRef.current?.grant()
    if (recorded) void recorded.then(settle)
    else settle()
  }, leadS)
  const callbacks = useRef(p)
  callbacks.current = p
  const router = useRouter({ warn: false })
  const me = useMe(p.ws).data
  const own = useClaudeConsent(p.ws, !p.consent && !!p.claudeCode && !!here && isOwner(here, me?.person))
  const consent = p.consent ?? own
  consentRef.current = consent
  const sequenceStart = p.action?.action === 'new_sequence' && !!p.action.launch
  // D2 grant terms come from the signer's conditional sequence, never the proposed browser action.
  const story = sequenceStart ? { signLabel: 'Sign and start agent', steps: [] } : storyOf(p.action, { startsAgent: p.startsAgent, person: me?.person })
  const canClick = clickReady(here) && (!p.action || clickEligible(p.action))
  const started = useRef(false)
  useLayoutEffect(() => {
    if (!viaHub && canClick && p.action && !p.children && (!p.controls || sequenceStart) && !p.hint && !started.current) {
      // Run-9 J04: "Create and start" opens straight on the host's review, so ONE press signs what is shown. Without
      // a trusted press it prepares only (pressed=false: no arm); the press in ClickSignPanel is still needed.
      const pressed = takeTrustedPress()
      if (!pressed && !sequenceStart) return
      started.current = true
      void click.start(p.action, 0, pressed)
    }
  })
  useEffect(() => {
    if (click.phase === 'cancelled' || (click.phase === 'failed' && ['prepare', 'retry', 'again', 'review'].includes(click.problem?.next ?? ''))) p.onInterrupted?.()
  }, [click.phase, click.problem, p.onInterrupted])
  useEffect(() => { callbacks.current.onPending?.(['preparing', 'viewing', 'ready', 'signing', 'settling'].includes(click.phase)) }, [click.phase])
  useEffect(() => { callbacks.current.onSigning?.(click.phase === 'signing') }, [click.phase])
  useEffect(() => () => callbacks.current.onSigning?.(false), [])
  useEffect(() => { if (click.phase === 'done' || click.phase === 'held' || click.switched || click.leftUnsettled) p.onClose() }, [click.phase, click.switched, click.leftUnsettled, p.onClose])
  // The slot counts as seen only when it is in the page, in this open popover, beside the click review that can be signed.
  // A popover folded away never sets it.
  const folded = !!p.onSigning && click.phase === 'signing'
  useEffect(() => {
    const el = unsignedEl.current
    if (click.phase !== 'ready' || folded || !el?.isConnected) return
    if (el.closest('[hidden]') || document.visibilityState === 'hidden') return
    unsignedShown.current = true
  }, [click.phase, folded])
  const reported = useRef<string | null>(null)
  useEffect(() => {
    const prepared = click.prepared
    if (click.phase !== 'failed' || click.problem?.next !== 'open' || !prepared || !p.ws || reported.current === prepared.request_id) return
    reported.current = prepared.request_id
    void api.stateClick(p.ws, prepared.request_id).then(got => {
      if (!got.key || !got.progress) return
      // The request this dialog prepared goes along, as for a signed one: callers take the key only from their own request.
      const { request } = settledAs(prepared, { state: 'submitted', duplicate: false, key: got.key })
      p.onSettled?.('failed', { id: prepared.request_id, state: 'submitted', op: prepared.op, key: got.key, expires_at: prepared.expires_at, event_ids: [], seq: null, progress: got.progress }, request, { via: 'click', unsignedShown: unsignedShown.current })
    }, () => {})
  }, [click.phase, click.problem, click.prepared, p.ws, p.onSettled])
  const [waitUntil, setWaitUntil] = useState(0)
  useEffect(() => { if (click.problem?.waitMs) setWaitUntil(Date.now() + click.problem.waitMs) }, [click.problem])
  const close = async () => {
    if (click.prepared && click.problem?.next !== 'check') await click.cancel()
    p.onClose()
  }
  const dismiss = (label: 'Cancel' | 'Close') => <Button ref={cancelRef} variant="ghost" onClick={() => void close()}>{label}</Button>
  if (viaHub) {
    return <SignShell inline={p.inline} anchor={anchor} open onDismiss={p.onClose} title={<Inline>{p.title}</Inline>} description={null} showClose onOpenAutoFocus={e => e.preventDefault()}>
      {isAddonGrant(p.action) && p.ws ? <HubAddonPanel ws={p.ws} action={p.action} onDone={() => p.onSettled?.('done', null, null, hub)} /> : hubSettingsSplit(p.action) && p.ws ? <HubSettingsPanel ws={p.ws} action={p.action as Extract<HandoffAction, { action: 'settings_set' }>} onDone={() => p.onSettled?.('done', null, null, hub)} /> : null}
      <div className="flex justify-end gap-2"><Button variant="outline" ref={cancelRef} onClick={p.onClose}>Close</Button></div>
    </SignShell>
  }
  return <SignShell inline={p.inline} anchor={anchor} open={!folded} onDismiss={() => void close()} title={<SignerHeadline view={click.view?.view ?? {}} fallback={<Inline>{p.title}</Inline>} />} showClose onOpenAutoFocus={e => { e.preventDefault(); (p.initialFocus?.() ?? cancelRef.current)?.focus() }}>
    {(!sequenceStart || !click.view || click.view.view.risk_lines?.includes(DEPOSIT_RISK)) && p.controls}{p.lead}{!click.view && p.summary}
    {/* The switch is read before the press; a request already open keeps its review (the signer refuses it if the switch moved). */}
    {!canClick && click.phase === 'idle' ? <><p>One click is not ready on this computer.</p>{router && <Link to="/settings/$tab" params={{ tab: 'general' }} onClick={() => void close()}>Turn on One click</Link>}<div className="flex justify-end">{dismiss('Close')}</div></> : <>
      {click.phase === 'idle' && <>{p.children}{p.hint && <p>{p.hint}</p>}<div className="flex flex-wrap justify-end gap-2">{dismiss('Cancel')}<Button disabled={!p.action || !!p.hint} onClick={e => { if (trustedPress(e.nativeEvent) && p.action) void click.start(p.action) }}>{p.confirmLabel ?? story?.signLabel ?? p.title}</Button></div></>}
      {['preparing', 'viewing'].includes(click.phase) && <p role="status">Reading the review…</p>}
      {(click.phase === 'ready' || click.phase === 'signing') && click.view && <ClickSignPanel ws={p.ws} answer={click.view} problem={click.problem} signing={click.phase === 'signing'} onSign={() => void click.sign()} onCancel={() => void close()} onDisarm={() => void click.cancel()} pressed={click.fresh} waitUntil={waitUntil} story={story} label={p.confirmLabel} manyWorkspaces={many} />}
      {click.phase === 'settling' && <p role="status">The outcome is not certain yet. Do not sign again. {click.settleNote}</p>}
      {click.phase === 'failed' && click.problem && (click.problem.next === 'unavailable' ? <><p>{click.problem.body || click.problem.title}</p>{p.ws && click.problem.devices && <RefusalDevices ws={p.ws} ids={click.problem.devices} />}</> : <ClickProblemBox problem={click.problem} ws={p.ws} />)}
      {click.phase === 'cancelled' && <p role="status">Nothing was signed.</p>}
      {['cancelled', 'failed', 'settling'].includes(click.phase) && <div className="flex flex-wrap justify-end gap-2">
        {/* An ended or unsettled attempt can always be left: Cancel withdraws a request still open, Close just closes. */}
        {dismiss(click.phase === 'failed' && click.prepared && click.problem?.next !== 'check' ? 'Cancel' : 'Close')}
        {(click.phase === 'cancelled' || (click.phase === 'failed' && ['prepare', 'retry', 'again', 'review'].includes(click.problem?.next ?? ''))) && <Button onClick={e => { if (trustedPress(e.nativeEvent) && p.action) void click.again(p.action) }}>Try again</Button>}
      </div>}
      {(click.phase === 'ready' || click.phase === 'signing') && <ClaudeConsentLine c={consent} />}
      {p.unsigned && click.phase !== 'done' && <div ref={unsignedEl} data-testid="handoff-unsigned">{p.unsigned}</div>}
    </>}
  </SignShell>
}
