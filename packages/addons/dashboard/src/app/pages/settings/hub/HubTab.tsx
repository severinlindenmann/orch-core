import { useEffect, useState, type ReactNode } from 'react'
import { useRouter, useRouterState } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import type { Workspace } from '@/api/types'
import { hubQueries, hubServed, type HubStatus } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { Action, Details, Panel, hubTitle, useFullAllowed, useHub } from './shared'
import { JoinFlow, connectFromHash } from './JoinFlow'
import { FullSwitch } from './FullSwitch'
import { HubSharing } from './HubSharing'
import { LeaveFlow } from './LeaveFlow'
import { ShareRules, AlwaysShared } from './ShareRules'
import { NotTold } from './NotTold'
import { Network } from './Network'
import { HubBanner } from '@/app/shell/HubBanner'
import { useOpenHub, useRail } from '@/app/shell/CompanyRail'

/** Once connected, the one card: the hub's name, one sentence, and the actions in one row. Open hub is the same signed
 * one-hop ticket as the rail icon. Everything else (counts, the read-everything option, network) is behind one small link. */
function ConnectedCard({ ws, line, children, more }: { ws: string; line: ReactNode; children: ReactNode; more: ReactNode }) {
  const rail = useRail()
  const open = useOpenHub(ws)
  const canOpen = !(rail.data && !rail.data.hub) // the rail says there is no hub: nothing to open
  return <Panel title={hubTitle(rail.data?.hub?.name)}>{line}
    <div className="flex flex-wrap gap-2">
      {canOpen && <Action id="hub-open" variant="default" disabled={open.busy} onClick={() => open('/')}>{open.busy ? 'Opening hub…' : 'Open hub'}</Action>}
      {children}
    </div>
    <Details testId="hub-more" label="More about sharing">{more}</Details>
  </Panel>
}

export function HubTab({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const status = useHub(ws)
  const fullAllowed = useFullAllowed(ws)
  const [flow, setFlow] = useState<'join' | 'full' | 'leave' | 'leave-down' | 'share'>()
  // P13: the hub's /c page opens Settings > Hub with `#connect=<link>`. Take it once, take it out of the address, and open
  // the join dialog (standalone only) with the link prefilled.
  const router = useRouter()
  const hash = useRouterState({ select: s => s.location.hash })
  const [incoming] = useState(() => connectFromHash(hash))
  const [initialLink, setInitialLink] = useState<string>()
  useEffect(() => {
    if (/^#?connect=/.test(hash)) router.history.replace(router.history.location.pathname + router.history.location.search)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const mode = status.data?.mode
  useEffect(() => {
    if (incoming && mode === 'standalone') { setInitialLink(incoming); setFlow('join') }
  }, [incoming, mode])
  // The top-bar "Connect to hub" button links to `#connect`: the dialog opens empty (also when this page is already open).
  const wantsJoin = /^#?connect$/.test(hash)
  useEffect(() => {
    if (!wantsJoin || !mode) return
    if (mode === 'standalone') setFlow('join')
    router.history.replace(router.history.location.pathname + router.history.location.search)
  }, [wantsJoin, mode]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!hubServed(ws)) return <Panel title="Hub settings unavailable"><p>This host does not serve hub settings yet.</p></Panel>
  if (status.isPending) return <Panel title="Hub"><p role="status">Loading hub settings…</p></Panel>
  if (status.isError && !status.data) return <Panel title="Hub"><p role="alert">Could not load hub settings.</p><Action id="hub-status-retry" onClick={() => void status.refetch()}>Try again</Action></Panel>
  const s = status.data!
  // Older status responses have no display name; never substitute an id.
  const named = s as HubStatus & { name?: string; hub_name?: string }
  const hubName = named.hub_name ?? named.name
  if (s.mode === 'unknown') return <Panel title="Hub state not known"><p>The workspace has not reported its hub mode.</p></Panel>
  // The network settings, the hub's name and its id: in the connected pages they sit behind "More about sharing".
  const extra = <>
    <Network workspace={workspace} standalone={s.mode === 'standalone'} />
    {(s.mode === 'selective' || s.mode === 'full') && <HubSharing workspace={workspace} />}
    {hubName && <p className="text-sm font-semibold">Hub <Raw>{hubName}</Raw></p>}
    {s.hub_id && <InfoDrawer testId="hub-status-details"><p className="break-words text-sm">Hub id (from the hub, not signed): <Raw>{s.hub_id}</Raw></p></InfoDrawer>}
  </>
  // Keep active dialogs mounted when a refetch changes the mode, so their done screen remains visible.
  return <div className="space-y-4" data-testid="hub-tab">
    {flow === 'share' && s.mode === 'selective' ? <ShareRules ws={ws} workspace={workspace} onBack={() => setFlow(undefined)} /> : <>
      {s.not_told && <NotTold ws={ws} />}
      {s.reachable !== false && <HubBanner ws={ws} />}
      {(s.reseal_needed || s.key_change_pending || s.log_sync_stuck || s.link === 'joining' || s.link === 'switching') && <Panel title="Hub needs attention" tone="border-warning/40 bg-warning-soft"><p>The hub connection has changes in progress. Wait a few minutes, then reload this page. Make no changes here until this message is gone.</p></Panel>}
      {s.mode === 'standalone' && <StandalonePage onJoin={() => setFlow('join')} extra={extra} />}
      {s.reachable === false && (s.mode === 'selective' || s.mode === 'full') && <Panel title={hubTitle(hubName)}>
        <p>Hub unavailable. Your work continues on this computer.</p>
        <Action id="hub-check-again" variant="default" disabled={status.isFetching} onClick={() => void status.refetch()}>Check again</Action>
        {status.isError && <p role="alert">Could not check the hub. Try again.</p>}
        <Details testId="hub-more" label="More about sharing">
          <p>Sharing and phone approvals resume when the hub is back.</p>
          <Action id="hub-leave-down" variant="link" onClick={() => setFlow('leave-down')}>Hub gone for good? Leave without it</Action>
          {extra}
        </Details>
      </Panel>}
      {s.reachable !== false && s.mode === 'selective' && <SelectivePage ws={ws} fullAllowed={fullAllowed} onShare={() => setFlow('share')} onFull={() => setFlow('full')} onLeave={() => setFlow('leave')} extra={extra} />}
      {s.reachable !== false && s.mode === 'full' && <FullPage ws={ws} status={s} onLeave={() => setFlow('leave')} extra={extra} />}
    </>}
    {flow === 'join' && <JoinFlow ws={ws} initialLink={initialLink} onClose={() => setFlow(undefined)} />}
    {flow === 'full' && <FullSwitch ws={ws} onClose={() => setFlow(undefined)} />}
    {flow === 'leave-down' && <LeaveFlow ws={ws} hubDown onClose={() => setFlow(undefined)} />}
    {flow === 'leave' && <LeaveFlow ws={ws} onClose={() => setFlow(undefined)} />}
  </div>
}
export function StandalonePage({ onJoin, extra }: { onJoin: () => void; extra?: ReactNode }) {
  return <Panel title="Optional: connect this workspace to your company hub">
    <Action id="hub-connect" variant="default" onClick={onJoin}>Connect a hub</Action>
    <Details testId="hub-standalone-details">
      <p>This workspace works on its own. Your tickets, agents and keys stay on this computer.</p>
      <p>You can leave the hub at any time and keep working.</p>
      <p>You need a connect link from your hub admin.</p>
      {extra}
    </Details>
  </Panel>
}
export function SelectivePage({ ws, fullAllowed: _fullAllowed, onShare, onFull, onLeave, extra }: { ws: string; fullAllowed: boolean; onShare: () => void; onFull: () => void; onLeave: () => void; extra?: ReactNode }) {
  const share = useQuery(hubQueries.share(ws))
  const rules = share.data?.available ? share.data.rules.filter(r => r.kind !== 'needs_you' && r.kind !== 'questions').length : 0
  const more = <>
    {share.isPending ? <p role="status">Loading shared items…</p> : share.isError ? <><p role="alert">Could not load shared items.</p><Action id="hub-shared-retry" onClick={() => void share.refetch()}>Try again</Action></> : !share.data.available ? <p data-testid="hub-share-unavailable">Sharing settings are not available on this host yet. Update orch to choose what the hub sees.</p> : <>
      <p>{share.data.shared} shared {share.data.shared === 1 ? 'item' : 'items'}</p>
      <p>{rules} sharing {rules === 1 ? 'rule' : 'rules'}</p>
      <AlwaysShared />
      <p>These numbers come from the hub; orch cannot check them.</p>
      {share.data.refused.length > 0 && <p className="text-warning">{share.data.refused.length} items could not be shared.</p>}
    </>}
    <div className="space-y-2 border-t border-border pt-3"><h3 className="font-semibold">Let the hub read everything</h3>
      <p>The hub will read every ticket, file and note. Hub admins become admins here. Going back requires leaving and rejoining.</p>
      <p>You review the change here and confirm it with one click.</p><Action id="hub-full-switch" onClick={onFull}>Let the hub read everything</Action>
    </div>
    {extra}
  </>
  return <ConnectedCard ws={ws} line={<p>Connected. The hub sees only what you share, plus this workspace’s name.</p>} more={more}>
    {share.data?.available !== false && <Action id="hub-choose-share" onClick={onShare}>Choose what to share</Action>}
    <Action id="hub-leave" variant="link" onClick={onLeave}>Leave the hub</Action>
  </ConnectedCard>
}
export function FullPage({ ws, status, onLeave, extra }: { ws: string; status: HubStatus; onLeave: () => void; extra?: ReactNode }) {
  const more = <>
    <div className="space-y-2"><h3 className="font-semibold">Access through the hub</h3><Details testId="hub-access-details"><p>These numbers come from the hub; orch cannot check them.</p></Details>
      {(['read', 'edit', 'admin'] as const).map(role => <div key={role} className="flex flex-wrap justify-between gap-2 border-t border-border py-2"><span>{role === 'read' ? 'Can read' : role === 'edit' ? 'Can edit' : 'Admins'}</span><span>{status.groups ? (status.groups[role] === 1 ? '1 person' : status.groups[role] + ' people') : 'Count not available'}</span></div>)}
    </div>
    <p>Every ticket, file and note, now and later. Hub admins act as admins here. Approvals use your phone or passkey.</p>
    <p>To share less, leave the hub and join again; what the hub already read cannot be taken back.</p>
    {extra}
  </>
  return <ConnectedCard ws={ws} line={<p><strong>The hub can read everything in this workspace</strong>. Your work stays on this computer.</p>} more={more}>
    <Action id="hub-leave" variant="link" onClick={onLeave}>Leave the hub</Action>
  </ConnectedCard>
}
