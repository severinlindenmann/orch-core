import { useState } from 'react'
import { Action, Flow, NoOneClick, caveat, useHubRefresh, useOneClick } from './shared'
import { InfoDrawer } from '@/components/InfoDrawer'
import { HubStep } from '../host/HubStep'

// Leaving uses HubStep while One click is on. The terminal path remains for an unreachable hub.
export function LeaveFlow({ ws, hubDown = false, onClose }: { ws: string; hubDown?: boolean; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const refresh = useHubRefresh(ws)
  const terminal = hubDown
  const [failed, setFailed] = useState(false)
  const consequences = <div className="space-y-2">
    <p>Your work stays on this computer. You can keep working locally.</p>
    <p>Phone access, publishing and handoffs stop. Hub members lose access here.</p>
    <p className="flex items-center gap-1 text-text-muted">{caveat}
      {hubDown && <InfoDrawer label="More about leaving"><p>The hub will not be told until it is reachable. It may still list this workspace and keep copies of what it read.</p></InfoDrawer>}</p>
  </div>
  const here = !terminal && oneClick === 'on'
  return <Flow title={hubDown ? 'Leave without the hub' : 'Leave the hub'} onClose={onClose} finish={hubDown ? 'terminal' : 'here'}>
    {terminal ? <p>One click is not ready for leaving while the hub is unreachable.</p> : <>
      {!failed && consequences}
      {here
        ? <HubStep ws={ws} action={{ action: 'hub_leave' }} onClose={onClose} onFailedChange={setFailed} onChanged={refresh} doneText="You left the hub. This workspace works on its own now." />
        : <><NoOneClick state={oneClick} what="Leaving" /><div className="flex justify-end"><Action id="hub-leave-cancel" onClick={onClose}>Cancel</Action></div></>}
    </>}
  </Flow>
}
