// Hub settings > Network (R6 #1594): the company proxy for the hub connection. Only for an owner on this computer;
// the page says nothing until the small "Network" link is opened. The password is sent once and never shown again.
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { networkQueries } from '@/api/network'
import { ApiError, type Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Details, Field, Panel } from './shared'
import { useViewerRole } from '../host/useViewerRole'

export function Network({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const owner = useViewerRole(workspace) === 'owner'
  const qc = useQueryClient()
  const got = useQuery({ ...networkQueries.get(ws), enabled: owner })
  const [proxy, setProxy] = useState('')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const save = useMutation({
    mutationFn: (clear: boolean) => api.putHubNetwork(ws, clear ? { proxy: null } : { proxy: proxy.trim(), user: user.trim() || null, password: password || null }),
    onSuccess: () => { setPassword(''); void qc.invalidateQueries({ queryKey: ['hub', ws, 'network'] }) },
  })
  if (!owner || !got.data) return null
  const d = got.data
  return (
    <Panel title="Network">
      <p data-testid="network-state">{d.broken ? "The network settings can't be read. The hub is not contacted until you fix or remove them." : d.proxy ? `The hub connection goes through your company proxy ${d.proxy}.` : 'This computer reaches the hub directly.'}</p>
      <Details testId="network-details">
        <Field label="Company proxy" testId="network-proxy" value={proxy} hint="Like https://proxy.office:3128. Leave it empty if you do not use one." onChange={setProxy} />
        <Field label="User name (if the proxy asks)" testId="network-user" value={user} onChange={setUser} />
        {user && <div className="space-y-1"><label className="block text-sm font-medium" htmlFor="network-password">Password</label>
          <input id="network-password" data-testid="network-password" type="password" autoComplete="off" className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm" value={password} onChange={(e) => setPassword(e.target.value)} />
          {proxy.startsWith('http://') && <p className="text-xs">This proxy is reached over plain http, so the password crosses your office network readable.</p>}</div>}
        <div className="flex flex-wrap items-center gap-3">
          <Button data-testid="network-save" disabled={!proxy.trim() || save.isPending} onClick={() => save.mutate(false)}>Save</Button>
          {(d.proxy || d.broken) && <Button variant="outline" data-testid="network-clear" disabled={save.isPending} onClick={() => save.mutate(true)}>Use no proxy</Button>}
          {save.isError && <span role="alert" data-testid="network-error">{save.error instanceof ApiError ? save.error.message : 'Could not save the network settings.'}</span>}
        </div>
        <p>It is a setting of this computer only. The hub never sets it, and the connection is still checked against the hub's key.</p>
      </Details>
    </Panel>
  )
}
