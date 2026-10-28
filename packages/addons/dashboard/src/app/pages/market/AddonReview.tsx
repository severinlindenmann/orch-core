import { toast } from 'sonner'
import { requestReverse } from '@/components/sign/ReverseUndoHost'
import { useState } from 'react'
import type { Workspace } from '@/api/types'
import type { AddOnRow } from '@/api/addOnSwitches'
import type { SignContent } from '@/api/signing'
import { hubApproves } from '@/api/hub'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { useHub } from '../settings/hub/shared'
/** Presentation only: the host's lines, notes and risk are shown as sent (the trust-line rule lives in the host's signcontent.py). */
export function capabilityWords(value: string): string {
  const names: Record<string, string> = { files: 'files', filesystem: 'files', read_files: 'read files', write_files: 'change files', pty: 'terminals', network: 'internet', spawn_agent: 'agents', serve_http: 'web pages', git_push: 'push code', choose_model: 'choose agent models', credential: 'saved sign-in keys', secrets: 'service tokens' }
  return names[value] ?? value
}

export function addonContent(content: SignContent, _marketplace?: boolean): SignContent {
  const lines = content.summary?.lines.filter(l => !l.startsWith('Version ')).map(l => l.startsWith('It says it will ') ? l.replace(/\b[a-z][a-z_]+\b/g, capabilityWords) : l)
  return content.summary && lines ? { ...content, summary: { ...content.summary, lines } } : content
}

export function AddonReview({ workspace, row, onBusy, onDone, onClose }: {
  workspace: Workspace; row: AddOnRow; marketplace: boolean; onBusy: (busy: boolean) => void; onDone: () => void; onClose: () => void
}) {
  const turningOn = row.state !== 'on'
  const title = `${turningOn ? 'Turn on' : 'Turn off'} ${row.title}`
  // Local signing is the default in every hub mode; the hub approval (FB15) is an optional extra the person asks for,
  // offered only where the host says this workspace is joined to a hub in full mode, and only to turn an add-on on.
  const [viaHub, setViaHub] = useState(false)
  const hubOffered = turningOn && hubApproves(useHub(workspace.id).data)
  const [signing, setSigning] = useState(false)
  if (viaHub) return <SignPopover workspace={workspace} ws={workspace.id} title={title} action={{ action: 'addon_grant', name: row.id }} inline forceHub onClose={onClose} onSettled={phase => { if (phase === 'done') onDone() }} />
  return <>
    <SignPopover workspace={workspace} ws={workspace.id} title={title} action={{ action: turningOn ? 'addon_grant' : 'addon_disable', name: row.id }} inline onSigning={busy => { setSigning(busy); onBusy(busy) }} onClose={onClose} doneToast={turningOn ? undefined : ''} onSettled={phase => { if (phase === 'done') {
      if (!turningOn) toast.success(`${row.title} is off`, { action: { label: 'Undo', onClick: () => requestReverse({ ws: workspace.id, action: { action: 'addon_grant', name: row.id } }) } })
      onDone()
    } }} />
    {hubOffered && <div className="flex justify-end"><Button variant="ghost" disabled={signing} onClick={() => setViaHub(true)}>Approve in the hub instead</Button></div>}
  </>
}
