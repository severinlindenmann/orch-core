// Settings > Add-ons > Publish (real host): where shared files are sent and where a link opens, and whether the server
// knows this workspace. Changing the addresses is a click-signed settings change (the host checks both addresses again);
// registering is a plain owner click on this computer. Plain words first; the ids the server's admin needs sit under Technical details.
import { useQuery } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { adminCommand, isUnknownWorkspace, newIdem, reachNote, shareError, shareQueries, sharesApi } from '@/api/shares'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { copyText } from '@/app/copyLink'
import type { Workspace } from '@/api/types'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useSettingsHandoff } from './useSettingsHandoff'

const ORIGIN = /^(?:https:\/\/[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[1-9][0-9]{0,4})?|http:\/\/(?:localhost|127\.0\.0\.1|10\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|192\.168\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))(?::[1-9][0-9]{0,4})?)$/

type Connection = 'idle' | 'busy' | 'ok' | 'fail'
type Registration = { at: 'unknown' } | { at: 'busy' } | { at: 'ok'; result: string } | { at: 'refused'; text: string; admin: boolean }

export function PublishServer({ workspace, owner, embedded = false }: { workspace: Workspace; owner: boolean; embedded?: boolean }) {
  const ws = workspace.id
  const list = useQuery(shareQueries.list(ws))
  const data = list.data && list.data.available ? list.data : null
  const [editing, setEditing] = useState(false)
  const [server, setServer] = useState('')
  const [share, setShare] = useState('')
  const [copyNote, setCopyNote] = useState('')
  const [conn, setConn] = useState<Connection>('idle')
  const [reg, setReg] = useState<Registration>({ at: 'unknown' })
  const { ask, dialog } = useSettingsHandoff(ws, () => void list.refetch())
  if (list.isError)
    return (
      <Section title="Publish: where files go">
        <p role="alert" className="text-[13px] text-danger" data-testid="publish-server-error">
          {shareError(list.error)}
        </p>
        <Button size="sm" variant="ghost" onClick={() => void list.refetch()}>
          Try again
        </Button>
      </Section>
    )
  if (!data) return null
  const { ready } = publishValidation(server, share)
  // Read-only probe: ask the host for the shares list again; it says whether the server answered. Nothing is written.
  const check = async () => {
    setConn('busy')
    const got = await list.refetch()
    setConn(got.data && got.data.available && got.data.live ? 'ok' : 'fail')
  }
  const register = async () => {
    setReg({ at: 'busy' })
    try {
      const got = await sharesApi.register(ws, newIdem())
      setReg({ at: 'ok', result: got.result })
    } catch (e) {
      setReg({ at: 'refused', text: shareError(e), admin: isUnknownWorkspace(e) })
    }
  }
  return (
    <Wrap embedded={embedded}>
      <div className="space-y-2 text-[13px]" data-testid="publish-server">
        <p className="text-text-muted">
          When you share files, they are sent to a publish server you choose, and the link opens on a second address. Only a person changes these addresses, with one click here.
        </p>
        {data.configured ? (
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
            <dt className="text-text-muted">Files are sent to</dt>
            <dd className="break-all">{data.server ? <Inline>{data.server}</Inline> : 'not shown'}</dd>
            <dt className="text-text-muted">Links open at</dt>
            <dd className="break-all">{data.share ? <Inline>{data.share}</Inline> : 'not shown'}</dd>
          </dl>
        ) : (
          <p className="text-warning">No server is set up yet, so nothing can be shared.</p>
        )}
        {data.configured && reachNote(data.server, data.share) && (
          <p data-testid="publish-local-note" className="text-text-muted">
            {reachNote(data.server, data.share)}
          </p>
        )}
        {owner ? (
          <Button size="sm" variant="secondary" onClick={() => { setServer(data.server ?? ''); setShare(data.share ?? ''); setEditing(true) }}>
            {data.configured ? 'Edit server' : 'Set up'}
          </Button>
        ) : (
          <OwnerNote>Only owners can change this.</OwnerNote>
        )}
        {data.configured && (
          <div className="space-y-1 border-t border-border pt-2" data-testid="publish-connection">
            <p className="font-medium">Is the server reachable?</p>
            <p role="status" className="text-text-muted">
              {conn === 'idle' && 'Not checked.'}
              {conn === 'busy' && 'Checking…'}
              {conn === 'ok' && 'Connected. The server answered.'}
              {conn === 'fail' && 'Could not connect. The server is not running or cannot be reached from this computer. Check the address, or ask your workspace administrator.'}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" disabled={conn === 'busy'} onClick={() => void check()}>
                Check connection
              </Button>
              <Button size="sm" variant="ghost" asChild>
                <Link to="/shares">Open Shares</Link>
              </Button>
            </div>
          </div>
        )}
        {data.configured && (
          <div className="space-y-1 border-t border-border pt-2" data-testid="publish-registration">
            <p className="font-medium">Does the server know this workspace?</p>
            <p role="status" className="text-text-muted">
              {reg.at === 'unknown' && 'Not checked yet. It is checked by itself the first time you share; you can check now.'}
              {reg.at === 'busy' && 'Checking…'}
              {reg.at === 'ok' && (reg.result === 'unchanged' ? 'Yes, the server already knew this workspace.' : 'Yes, the server now knows this workspace.')}
              {reg.at === 'refused' && reg.text}
            </p>
            {reg.at === 'refused' && reg.admin && (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => void copyText(`Please allow this workspace on the sharing server:\n${adminCommand(data.workspace, data.owner)}`).then((ok) => setCopyNote(ok ? 'Setup details copied.' : 'Could not copy. Press the i and copy the line by hand.'))}
                  >
                    Copy setup details for the server administrator
                  </Button>
                  <span role="status" className="text-[12px] text-text-muted">
                    {copyNote}
                  </span>
                </div>
                <InfoDrawer>
                  <p className="mt-1">The server’s admin runs:</p>
                  <p className="break-all font-mono">{adminCommand(data.workspace, data.owner)}</p>
                </InfoDrawer>
              </>
            )}
            {owner && (
              <Button size="sm" variant="ghost" disabled={reg.at === 'busy'} onClick={() => void register()}>
                {reg.at === 'unknown' ? 'Check now' : 'Check again'}
              </Button>
            )}
          </div>
        )}
      </div>
      {editing && (
        <Dialog open onOpenChange={(o) => !o && setEditing(false)}>
          <DialogContent className="max-w-md gap-3 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Where shared files go</DialogTitle>
              <DialogDescription>Ask your workspace administrator for the publish server address. You will see both addresses again when you sign. Files you share later go to the first address, and links open at the second.</DialogDescription>
            </DialogHeader>
            <PublishForm server={server} share={share} setServer={setServer} setShare={setShare} />
            <DialogFooter>
              <Button variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
              <Button
                disabled={!ready}
                onClick={() => {
                  setEditing(false)
                  ask({ title: 'Choose where shared files go', action: { action: 'publish_setup', server, share },  })
                }}
              >
                Review and sign
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {dialog}
    </Wrap>
  )
}

export function publishValidation(server: string, share: string) {
  const bad = (v: string) => v !== '' && !ORIGIN.test(v)
  const problem = bad(server)
    ? 'The files address must start with https:// (or http://127.0.0.1 for this computer), in lower case, with no path.'
    : bad(share)
      ? 'The links address must start with https:// (or http://127.0.0.1 for this computer), in lower case, with no path.'
      : server && server === share
        ? 'The two addresses must differ: opened files get their own address.'
        : ''
  const ready = ORIGIN.test(server) && ORIGIN.test(share) && server !== share
  return { problem, ready }
}

export function PublishForm({ server, share, setServer, setShare }: { server: string; share: string; setServer: (value: string) => void; setShare: (value: string) => void }) {
  const { problem } = publishValidation(server, share)
  return (
    <div className="space-y-3 text-[13px]">
      <div className="space-y-1.5">
        <Label htmlFor="pub-server">Files are sent to</Label>
        <input id="pub-server" className="h-8 w-full rounded border border-border bg-bg px-2 font-mono text-[12px]" value={server} onChange={(e) => setServer(e.target.value.trim())} placeholder="https://pub.example.org" />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="pub-share">Links open at</Label>
        <input id="pub-share" className="h-8 w-full rounded border border-border bg-bg px-2 font-mono text-[12px]" value={share} onChange={(e) => setShare(e.target.value.trim())} placeholder="https://apps.example.org" />
      </div>
      {problem && <p role="alert" className="text-danger">{problem}</p>}
    </div>
  )
}

function Wrap({ embedded, children }: { embedded: boolean; children: React.ReactNode }) {
  return embedded ? <div className="mt-2 rounded-md border border-border bg-bg/40 p-3">{children}</div> : <Section title="Publish: where files go">{children}</Section>
}
