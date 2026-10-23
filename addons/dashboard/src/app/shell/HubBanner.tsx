import { useState } from 'react'
import { Action, Panel, useHub } from '../pages/settings/hub/shared'
import { LeaveFlow } from '../pages/settings/hub/LeaveFlow'

export function HubBanner({ ws }: { ws?: string }) {
  const status = useHub(ws)
  const [leave, setLeave] = useState(false)
  const [error, setError] = useState(false)
  const s = status.data
  if (!ws || !s || s.mode === 'standalone' || s.mode === 'unknown' || s.reachable !== false) return null
  return <div data-testid="hub-offline-banner" className="mb-4">
    <Panel title={s.mode === 'full' ? 'Approvals are paused: hub offline (full access)' : 'Hub offline, working locally'} tone="border-warning/40 bg-warning-soft">
      <p>{s.mode === 'full' ? 'Your work is here, but approvals need the hub. Wait for it to return, or leave without it.' : 'You can keep working and signing locally. Shared updates wait until the hub is reachable.'}</p>
      <div className="flex flex-wrap gap-2"><Action id="hub-check-again" disabled={status.isFetching} onClick={() => { void status.refetch().then(r => setError(r.isError)) }}>Check again</Action>
        <Action id="hub-leave-down" onClick={() => setLeave(true)}>Hub gone for good? Leave without it</Action></div>
      {error && <p role="alert">Could not check the hub. Try again.</p>}
    </Panel>
    {leave && <LeaveFlow ws={ws} hubDown onClose={() => setLeave(false)} />}
  </div>
}
