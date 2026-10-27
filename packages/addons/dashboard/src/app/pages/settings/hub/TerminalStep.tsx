import { useEffect, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { hubQueries, type HubCommandRequest, type HubStatus } from '@/api/hub'
import { ExactCommandLine } from '@/components/sign/HandoffPanel'
import { Raw } from '@/components/sign/visible'
import { Action } from './shared'

/** This POST only constructs a command. A successful response never means the change happened. */
export function TerminalStep({ ws, request, children, onStatus }: { ws: string; request: HubCommandRequest; children: ReactNode; onStatus?: (status: HubStatus) => boolean }) {
  const qc = useQueryClient()
  const [command, setCommand] = useState<string>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [checking, setChecking] = useState(false)
  const [notice, setNotice] = useState('')
  const encoded = JSON.stringify(request)
  useEffect(() => {
    let alive = true
    setCommand(undefined); setError('')
    void api.getHubCommand(ws, JSON.parse(encoded) as HubCommandRequest).then(
      result => { if (alive) setCommand(result.command) },
      e => { if (alive) setError(e instanceof ApiError && e.status === 400 ? 'Check the values you entered. The invite code, file path or sharing rule was not accepted. Go back to correct them.' : 'Could not prepare the command. Try again.') },
    )
    return () => { alive = false }
  }, [ws, encoded, retry])
  async function check() {
    setChecking(true); setNotice('')
    try {
      const status = await qc.fetchQuery({ ...hubQueries.status(ws), staleTime: 0 })
      await qc.invalidateQueries({ queryKey: hubQueries.share(ws).queryKey })
      if (!onStatus?.(status)) setNotice('No confirmed change yet. If you ran the command, wait a moment and check again.')
    } catch { setNotice('Could not check the workspace. Try again.') }
    finally { setChecking(false) }
  }
  return <div className="space-y-4">
    {children}
    <p>The terminal shows what you are signing; check it matches this screen.</p>
    <p className="text-text-muted">Finish on your computer: open a terminal on the computer that runs this workspace. This dashboard is not where you sign: it never sees your passphrase or your key.</p>
    <h3 className="font-semibold">Copy this command into the terminal</h3>
    {command ? <ExactCommandLine command={command} testId="hub-command" /> : error ? <div role="alert"><p className="text-danger">{error}</p><Action id="hub-command-retry" onClick={() => setRetry(n => n + 1)}>Try again</Action></div> : <p role="status">Preparing command…</p>}
    <InfoDrawer testId="hub-technical-details"><div className="mt-2 space-y-2 break-all"><p>Workspace: <Raw>{ws}</Raw></p><Raw>{JSON.stringify(request, null, 2)}</Raw></div></InfoDrawer>
    {command && <Action id="hub-ran-it" disabled={checking} onClick={() => void check()}>{checking ? 'Checking…' : 'I ran it'}</Action>}
    {notice && <p role="status">{notice}</p>}
  </div>
}
