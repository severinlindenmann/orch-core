// Settings > Add-ons > Publish (real host): where shared files are sent and where a link opens, and whether the server
// knows this workspace. Changing the addresses is a signed settings change (terminal only); registering is a plain
// request. Plain words first; the ids the server's admin needs sit under Technical details.
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { adminCommand, isUnknownWorkspace, newIdem, shareError, shareQueries, sharesApi } from '@/api/shares'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import type { Workspace } from '@/api/types'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useSettingsHandoff } from './useSettingsHandoff'

const ORIGIN = /^(?:https:\/\/[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[1-9][0-9]{0,4})?|http:\/\/(?:localhost|127\.0\.0\.1)(?::[1-9][0-9]{0,4})?)$/

type Registration = { at: 'unknown' } | { at: 'busy' } | { at: 'ok'; result: string } | { at: 'refused'; text: string; admin: boolean }

export function PublishServer({ workspace, owner }: { workspace: Workspace; owner: boolean }) {
  const ws = workspace.id
  const list = useQuery(shareQueries.list(ws))
  const data = list.data && list.data.available ? list.data : null
  const [editing, setEditing] = useState(false)
  const [server, setServer] = useState('')
  const [share, setShare] = useState('')
  const [reg, setReg] = useState<Registration>({ at: 'unknown' })
  const { ask, dialog } = useSettingsHandoff(ws, () => void list.refetch())
  if (!data) return null
  const bad = (v: string) => v !== '' && !ORIGIN.test(v)
  const problem = bad(server)
    ? 'The files address must start with https:// (or http://127.0.0.1 for this computer), in lower case, with no path.'
    : bad(share)
      ? 'The links address must start with https:// (or http://127.0.0.1 for this computer), in lower case, with no path.'
      : server && server === share
        ? 'The two addresses must differ: opened files get their own address.'
        : ''
  const ready = ORIGIN.test(server) && ORIGIN.test(share) && server !== share
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
    <Section title="Publish: where files go">
      <div className="space-y-2 text-[13px]" data-testid="publish-server">
        <p className="text-text-muted">
          When you share files, they are sent to a publish server you choose, and the link opens on a second address. Only a person changes these addresses, and you sign the change in your terminal.
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
        {owner ? (
          <Button size="sm" variant="secondary" onClick={() => { setServer(data.server ?? ''); setShare(data.share ?? ''); setEditing(true) }}>
            {data.configured ? 'Change addresses' : 'Set up'}
          </Button>
        ) : (
          <OwnerNote>Only owners can change this.</OwnerNote>
        )}
        {data.configured && (
          <div className="space-y-1 border-t border-border pt-2" data-testid="publish-registration">
            <p className="font-medium">Does the server know this workspace?</p>
            <p role="status" className="text-text-muted">
              {reg.at === 'unknown' && 'Not checked yet. It is checked by itself the first time you share; you can check now.'}
              {reg.at === 'busy' && 'Checking…'}
              {reg.at === 'ok' && (reg.result === 'unchanged' ? 'Yes, the server already knew this workspace.' : 'Yes, the server now knows this workspace.')}
              {reg.at === 'refused' && (reg.admin ? 'The publish server does not know this workspace yet.' : reg.text)}
            </p>
            {reg.at === 'refused' && reg.admin && (
              <>
                <p>Ask its admin to allow this workspace, then check again.</p>
                <details className="text-[12px] text-text-muted">
                  <summary className="cursor-pointer select-none">Technical details</summary>
                  <p className="mt-1">The server’s admin runs:</p>
                  <p className="break-all font-mono">{adminCommand(data.workspace, data.owner)}</p>
                </details>
              </>
            )}
            {owner && (
              <Button size="sm" variant="ghost" disabled={reg.at === 'busy'} onClick={() => void register()}>
                Check now
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
              <DialogDescription>You will see both addresses again when you sign. Files you share later go to the first address, and links open at the second.</DialogDescription>
            </DialogHeader>
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
            <DialogFooter>
              <Button variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
              <Button
                disabled={!ready}
                onClick={() => {
                  setEditing(false)
                  ask({ title: 'Choose where shared files go', action: { action: 'publish_setup', server, share }, autoStart: true })
                }}
              >
                Continue to sign
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      {dialog}
    </Section>
  )
}
