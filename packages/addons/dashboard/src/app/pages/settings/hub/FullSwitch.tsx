import { useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { InfoDrawer } from '@/components/InfoDrawer'
import { ApiError } from '@/api/types'
import { Raw } from '@/components/sign/visible'
import { newIdem } from '@/api/runs'
import { HubStep } from '../host/HubStep'
import { Action, Flow, NoOneClick, useHub, useHubRefresh, useOneClick } from './shared'

// Run-9 feedback 6: letting the hub read everything is one click here. The card is the one the host pinned at the join;
// after the signed switch the host tells the hub (hub.connect without an invite) and seals the workspace key to it.
export function FullSwitch({ ws, onClose }: { ws: string; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const status = useHub(ws).data
  const [signed, setSigned] = useState(false)
  const [failed, setFailed] = useState(false)
  const card = status?.hub_card ?? null
  const review = <div className="space-y-2">
    <p>Hub admins can read every ticket, file and note.</p>
    <p>Hub admins become admins here.</p>
    <p>You'll approve on your phone or passkey.</p>
    <p className="text-warning">To share less, leave and rejoin; what the hub already read cannot be taken back.</p>
  </div>
  if (signed && card) return <Flow title="Let the hub read everything" onClose={onClose} finish="here"><Connect ws={ws} card={card} onClose={onClose} /></Flow>
  return <Flow title="Let the hub read everything" onClose={onClose} finish="here">
    {!failed && review}
    {!status?.hub_id || !card ? <p role="alert" data-testid="hub-full-nocard">The host did not give this page the hub it joined. Reload, or leave the hub and connect again.</p>
      : oneClick !== 'on' ? <NoOneClick state={oneClick} what="Switching" />
      : <HubStep ws={ws} action={{ action: 'hub_mode_full', hub_card: card, hub_id: status.hub_id }} onClose={onClose} onFailedChange={setFailed} onChanged={() => { setSigned(true) }} />}
    {(!status?.hub_id || !card || oneClick !== 'on') && <div className="flex justify-end"><Action id="hub-full-cancel" onClick={onClose}>Cancel</Action></div>}
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
      e => { setWhy(e instanceof ApiError && e.message ? e.code + ': ' + e.message : 'The hub did not answer.'); setState('failed') },
    )
  }
  useEffect(() => { if (!started.current) { started.current = true; run() } }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="space-y-3" data-testid="hub-full-stage">
    {state === 'working' && <p role="status">Signed. Telling the hub…</p>}
    {state === 'done' && <><p role="status" data-testid="hub-full-done">The hub can now read everything in this workspace.</p><div className="flex justify-end"><Action id="hub-full-close" onClick={onClose}>Close</Action></div></>}
    {state === 'failed' && <div role="alert" className="space-y-2"><p className="text-danger">Couldn't switch. Try again.</p><InfoDrawer><p><Raw>{why}</Raw></p></InfoDrawer><div className="flex justify-end gap-2"><Action id="hub-full-close" onClick={onClose}>Cancel</Action><Action id="hub-full-retry" onClick={run}>Try again</Action></div></div>}
  </div>
}
