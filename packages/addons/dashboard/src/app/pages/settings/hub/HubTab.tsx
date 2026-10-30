import { useEffect, useState } from 'react'
import { useRouter, useRouterState } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import type { Workspace } from '@/api/types'
import { hubQueries, hubServed, type HubStatus } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { Action, Details, Panel, useFullAllowed, useHub, violet } from './shared'
import { JoinFlow, connectFromHash } from './JoinFlow'
import { FullSwitch } from './FullSwitch'
import { LeaveFlow } from './LeaveFlow'
import { ShareRules, AlwaysShared } from './ShareRules'
import { NotTold } from './NotTold'
import { HubBanner } from '@/app/shell/HubBanner'
import { useOpenHub, useRail } from '@/app/shell/CompanyRail'

/** Once connected, the way in: the same signed one-hop ticket as the rail icon, as a visible button. */
function OpenHub({ ws }: { ws: string }) {
  const rail = useRail()
  const open = useOpenHub(ws)
  if (rail.data && !rail.data.hub) return null // the rail says there is no hub: nothing to open
  const name = rail.data?.hub?.name
  return <Panel title={name ? `${name} hub` : 'Hub'}><p>See your company's workspaces and what needs you.</p><Action id="hub-open" variant="default" onClick={() => open('/')}>Open hub</Action></Panel>
}

export function HubTab({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const status = useHub(ws)
  const fullAllowed = useFullAllowed(ws)
  const [flow, setFlow] = useState<'join' | 'full' | 'leave' | 'share'>()
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
  if (!hubServed(ws)) return <Panel title="Hub settings unavailable"><p>This host does not serve hub settings yet.</p></Panel>
  if (status.isPending) return <Panel title="Hub"><p role="status">Loading hub settings…</p></Panel>
  if (status.isError) return <Panel title="Hub"><p role="alert">Could not load hub settings.</p><Action id="hub-status-retry" onClick={() => void status.refetch()}>Try again</Action></Panel>
  const s = status.data
  // Older status responses have no display name; never substitute an id.
  const named = s as HubStatus & { name?: string; hub_name?: string }
  const hubName = named.hub_name ?? named.name
  if (s.mode === 'unknown') return <Panel title="Hub state not known"><p>The workspace has not reported its hub mode.</p></Panel>
  // Keep active dialogs mounted when a refetch changes the mode, so their done screen remains visible.
  return <div className="space-y-4" data-testid="hub-tab">
    {flow === 'share' && s.mode === 'selective' ? <ShareRules ws={ws} workspace={workspace} onBack={() => setFlow(undefined)} /> : <>
      {s.not_told && <NotTold ws={ws} />}
      <HubBanner ws={ws} />
      {(s.reseal_needed || s.key_change_pending || s.log_sync_stuck || s.link === 'joining' || s.link === 'switching') && <Panel title="Hub needs attention" tone="border-warning/40 bg-warning-soft"><p>The hub connection has work pending. Check its state in a terminal before making changes.</p></Panel>}
      {s.mode === 'standalone' && <StandalonePage onJoin={() => setFlow('join')} />}
      {(s.mode === 'selective' || s.mode === 'full') && <OpenHub ws={ws} />}
      {s.mode === 'selective' && <SelectivePage ws={ws} fullAllowed={fullAllowed} onShare={() => setFlow('share')} onFull={() => setFlow('full')} onLeave={() => setFlow('leave')} />}
      {s.mode === 'full' && <FullPage status={s} onLeave={() => setFlow('leave')} />}
      {hubName && <p className="text-sm font-semibold">Hub <Raw>{hubName}</Raw></p>}
      {s.hub_id && <InfoDrawer testId="hub-status-details"><p className="break-words text-sm">Hub id (from the hub, not signed): <Raw>{s.hub_id}</Raw></p></InfoDrawer>}
    </>}
    {flow === 'join' && <JoinFlow ws={ws} initialLink={initialLink} onClose={() => setFlow(undefined)} />}
    {flow === 'full' && <FullSwitch ws={ws} onClose={() => setFlow(undefined)} />}
    {flow === 'leave' && <LeaveFlow ws={ws} onClose={() => setFlow(undefined)} />}
  </div>
}
export function StandalonePage({ onJoin }: { onJoin: () => void }) {
  return <><Panel title="Hub"><p><strong>Not connected.</strong> This workspace works on its own. Connect a hub to use your phone, publish files and hand work to other workspaces.</p>
    <p className="text-text-muted">You need a connect link from your hub admin.</p>
    <Action id="hub-connect" onClick={onJoin}>Connect a hub</Action>
  </Panel><Panel title="What stays the same"><p>Your tickets, agents and keys stay on this computer.</p><p>You can leave the hub at any time and keep working.</p></Panel></>
}
export function SelectivePage({ ws, fullAllowed: _fullAllowed, onShare, onFull, onLeave }: { ws: string; fullAllowed: boolean; onShare: () => void; onFull: () => void; onLeave: () => void }) {
  const share = useQuery(hubQueries.share(ws))
  return <><Panel title="Connected · Hub sees only what you share" tone={violet}><p>You keep signing locally. Notes that need you and full questions are always shared; the hub sees nothing else, apart from this workspace's name, unless you choose it.</p></Panel>
    <Panel title="What the hub can see">
      {share.isPending ? <p role="status">Loading shared items…</p> : share.isError ? <><p role="alert">Could not load shared items.</p><Action id="hub-shared-retry" onClick={() => void share.refetch()}>Try again</Action></> : !share.data.available ? <p data-testid="hub-share-unavailable">Sharing settings are not available on this host yet. Update orch to choose what the hub sees.</p> : <>
        <p>{share.data.shared} shared items</p><Details testId="hub-shared-details"><p>Counts are from the hub, not signed.</p></Details><AlwaysShared share={share.data} />
        <p>{share.data.rules.filter(r => r.kind !== 'needs_you' && r.kind !== 'questions').length} sharing rules</p>
        {share.data.refused.length > 0 && <p className="text-warning">{share.data.refused.length} items could not be shared.</p>}
      </>}
      {share.data?.available !== false && <Action id="hub-choose-share" onClick={onShare}>Choose what to share</Action>}
    </Panel>
    <Panel title="Let the hub read everything"><p>The hub will read every ticket, file and note. Hub admins become admins here. Going back requires leaving and rejoining.</p>
      <p>You review the change here and confirm it with one click.</p><Action id="hub-full-switch" onClick={onFull}>Let the hub read everything</Action>
    </Panel>
    <Panel title="Leave the hub"><p>Keep working locally. Phone access, publishing and handoffs through the hub stop. You confirm here with one click.</p><Action id="hub-leave" onClick={onLeave}>Leave the hub</Action></Panel>
  </>
}
export function FullPage({ status, onLeave }: { status: HubStatus; onLeave: () => void }) {
  return <><Panel title="The hub can read everything in this workspace" tone={violet}><p>Every ticket, file and note, now and later. Hub admins act as admins here. Approvals use your phone or passkey.</p></Panel>
    <Panel title="Access through the hub"><Details testId="hub-access-details"><p>From the hub, not signed.</p></Details>
      {(['read', 'edit', 'admin'] as const).map(role => <div key={role} className="flex flex-wrap justify-between gap-2 border-t border-border py-2"><span>{role === 'read' ? 'Can read' : role === 'edit' ? 'Can edit' : 'Admins'}</span><span>{status.groups ? (status.groups[role] === 1 ? '1 person' : status.groups[role] + ' people') : 'Count not available'}</span></div>)}
    </Panel>
    <Panel title="Share only some things instead"><p>Leave this hub, then rejoin and choose “Only what I choose”. What the hub has already read cannot be taken back.</p></Panel>
    <Panel title="Leave the hub"><p>Your work stays on this computer. Hub members lose access through the hub. You confirm here with one click.</p><Action id="hub-leave" onClick={onLeave}>Leave the hub</Action></Panel>
  </>
}
