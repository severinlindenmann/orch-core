import { useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { newIdem } from '@/api/runs'
import { HubStep } from '../host/HubStep'
import { Action, Flow, NoOneClick, useHub, useHubRefresh, useOneClick } from './shared'

// Run-9 feedback 6: letting the hub read everything is one click here. The card is the one the host pinned at the join;
// after the signed switch the host tells the hub (hub.connect without an invite) and seals the workspace key to it.
export function FullSwitch({ ws, onClose }: { ws: string; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const status = useHub(ws).data
  const [signed, setSigned] = useState(false)
  const card = status?.hub_card ?? null
  const review = <><h3 className="font-semibold">Let the hub read everything</h3>
    <div className="divide-y divide-border rounded border border-border">
      {[['What the hub sees', 'Only shared items, notes and questions', 'Every ticket, file and note'], ['Who is an admin', 'Your workspace admins', 'Workspace admins and hub admins'], ['Approvals', 'Signed locally', 'Signed on your phone or passkey']].map(([label, before, after]) =>
        <div key={label} className="grid gap-2 p-3 @md/page:grid-cols-3"><strong>{label}</strong><p><span className="text-text-muted">Before: </span>{before}</p><p><span className="text-text-muted">After: </span>{after}</p></div>)}
    </div><p className="text-warning">One way: to share only some things again, leave the hub and rejoin. What the hub has already read cannot be taken back.</p></>
  return <Flow title="Let the hub read everything" onClose={onClose} finish="here">
    {review}
    {!status?.hub_id || !card ? <p role="alert" data-testid="hub-full-nocard">The host did not give this page the hub it joined. Reload, or leave the hub and connect again.</p>
      : signed ? <Connect ws={ws} card={card} onClose={onClose} />
      : oneClick !== 'on' ? <NoOneClick state={oneClick} what="Switching" />
      : <HubStep ws={ws} action={{ action: 'hub_mode_full', hub_card: card, hub_id: status.hub_id }} onClose={onClose} onChanged={() => { setSigned(true) }} />}
  </Flow>
}

function Connect({ ws, card, onClose }: { ws: string; card: string; onClose: () => void }) {
  const refresh = useHubRefresh(ws)
  const [state, setState] = useState<'working' | 'done' | 'failed'>('working')
  const [why, setWhy] = useState('')
  const started = useRef(false)
  const run = () => {
    setState('working'); setWhy('')
    api.connectHub(ws, { idem: newIdem(), hub_card: card }).then(
      () => { refresh(); onClose() }, // connected: the page shows full mode, nothing else is prepared
      e => { setWhy(e instanceof ApiError && e.message ? e.message : 'The hub did not answer.'); setState('failed') },
    )
  }
  useEffect(() => { if (!started.current) { started.current = true; run() } }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="space-y-3" data-testid="hub-full-stage">
    {state === 'working' && <p role="status">Signed. Telling the hub…</p>}
    {state === 'done' && <><p role="status" data-testid="hub-full-done">The hub can now read everything in this workspace.</p><div className="flex justify-end"><Action id="hub-full-close" onClick={onClose}>Close</Action></div></>}
    {state === 'failed' && <div role="alert" className="space-y-2"><p className="text-danger">Your switch is signed, but the hub did not take it yet. {why}</p><div className="flex gap-2"><Action id="hub-full-retry" onClick={run}>Try again</Action><Action id="hub-full-close" onClick={onClose}>Close</Action></div></div>}
  </div>
}
