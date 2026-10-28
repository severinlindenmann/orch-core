import { useState } from 'react'
import { Action, Flow, NoOneClick, caveat, useHubRefresh, useOneClick } from './shared'
import { InfoDrawer } from '@/components/InfoDrawer'
import { HubStep } from '../host/HubStep'

// Leaving is one click with the backup key (HubStep, hub_leave), also while the hub is unreachable: core's hub.leave is
// logged at once and the hub is told when it is reachable again.
export function LeaveFlow({ ws, hubDown = false, onClose }: { ws: string; hubDown?: boolean; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const refresh = useHubRefresh(ws)
  const [failed, setFailed] = useState(false)
  const consequences = <div className="space-y-2">
    <p>Your work stays on this computer. You can keep working locally.</p>
    <p>Phone access, publishing and handoffs stop. Hub members lose access here.</p>
    <p className="flex items-center gap-1 text-text-muted">{caveat}
      {hubDown && <InfoDrawer label="More about leaving"><p>The hub will not be told until it is reachable. It may still list this workspace and keep copies of what it read.</p></InfoDrawer>}</p>
  </div>
  return <Flow title={hubDown ? 'Leave without the hub' : 'Leave the hub'} onClose={onClose} finish="here">
    {!failed && consequences}
    {oneClick === 'on'
      ? <HubStep ws={ws} action={{ action: 'hub_leave' }} onClose={onClose} onFailedChange={setFailed} onChanged={refresh} doneText="You left the hub. This workspace works on its own now." />
      : <><NoOneClick state={oneClick} what="Leaving" /><div className="flex justify-end"><Action id="hub-leave-cancel" onClick={onClose}>Cancel</Action></div></>}
  </Flow>
}
