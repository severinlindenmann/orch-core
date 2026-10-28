import { useState } from 'react'
import { hubUrl } from '@/api/switch'
import type { Workspace } from '@/api/types'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Button } from '@/components/ui/button'
import { toastApiError } from '@/app/toast'
import { useHub } from '../hub/shared'
import { ReaderLevels } from './Readers'
import { ReplaceKey } from './ReplaceKey'

// Settings > Devices: phones connect through the company hub (owner decision 22 Oct, V3 hub vision); a workspace never pairs
// a phone directly. Joined to a hub: one button opens the hub's Phones page (its console pairing code) through the existing
// hub switch, so the browser is already signed in there. No hub: one quiet line and an "i", no button.

const WHY = 'Phones connect through your company hub: the hub shows a pairing code, and the phone then reaches this workspace through it. Without a hub there is nothing for a phone to connect to.'

function AddPhone({ ws }: { ws: string }) {
  const [busy, setBusy] = useState(false)
  const go = () => {
    setBusy(true)
    hubUrl(ws, 'open', '/phones').then((u) => location.assign(u)).catch((e: unknown) => { setBusy(false); toastApiError(e, 'The hub could not be reached.') })
  }
  return <Button size="sm" data-testid="add-phone" disabled={busy} onClick={go}>{busy ? 'Opening the hub…' : 'Add a phone'}</Button>
}

export function HostDevices({ workspace }: { workspace: Workspace }) {
  const hub = useHub(workspace.id)
  const mode = hub.data?.mode
  const joined = hub.data?.joined === true && (mode === 'selective' || mode === 'full')
  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <h2 className="text-base font-semibold">Phones</h2>
        {hub.isPending && hub.fetchStatus !== 'idle' ? null : joined ? (
          <AddPhone ws={workspace.id} />
        ) : (
          <p data-testid="devices-needs-hub" className="flex items-center gap-1.5 text-[13px] text-text-muted">
            <span>Phones connect through your company hub</span>
            <InfoDrawer testId="devices-needs-hub-info">{WHY}</InfoDrawer>
          </p>
        )}
      </div>
      <ReplaceKey workspace={workspace} />
      <ReaderLevels ws={workspace.id} />
    </div>
  )
}
