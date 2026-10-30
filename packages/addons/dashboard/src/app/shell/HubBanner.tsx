import { useState } from 'react'
import { Action, Panel, useHub } from '../pages/settings/hub/shared'
import { LeaveFlow } from '../pages/settings/hub/LeaveFlow'

export function HubBanner({ ws }: { ws?: string }) {
  const status = useHub(ws)
  const [leave, setLeave] = useState(false)
  const [error, setError] = useState(false)
  const s = status.data
  if (!ws || !s || s.mode === 'standalone' || s.mode === 'unknown') return null
  const ask = s.update_target ? <div data-testid="hub-update-banner" className="mb-4">
    <Panel title={'Your hub admin asks you to update orch to ' + s.update_target} tone="border-warning/40 bg-warning-soft">
      <p>Only you can do it: run <code>orch update</code> in a terminal on this computer. This notice goes away once orch is up to date.</p>
    </Panel>
  </div> : null
  if (s.reachable !== false) return ask
  return <>{ask}<div data-testid="hub-offline-banner" className="mb-4">
    <Panel title={'Hub offline, working locally'} tone="border-warning/40 bg-warning-soft">
      <p>You can keep working and signing here. Sharing and phone approvals resume when the hub is back.</p>
      <div className="flex flex-wrap gap-2"><Action id="hub-check-again" disabled={status.isFetching} onClick={() => { void status.refetch().then(r => setError(r.isError)) }}>Check again</Action>
        <Action id="hub-leave-down" onClick={() => setLeave(true)}>Hub gone for good? Leave without it</Action></div>
      {error && <p role="alert">Could not check the hub. Try again.</p>}
    </Panel>
    {leave && <LeaveFlow ws={ws} hubDown onClose={() => setLeave(false)} />}
  </div></>
}
