import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { hubQueries, hubServed } from '@/api/hub'
import { useWorkspace } from '@/app/workspace'
import { useViewerRole } from '../host/useViewerRole'
import type { Workspace } from '@/api/types'
import { AddRuleReview } from './AddRule'
import { useSharePreview } from './useSharePreview'
import type { HubWorkspace } from './StopSharing'
import { Action, Flow } from './shared'

export function SendSharesReview({ workspace, onClose }: { workspace: HubWorkspace; onClose: () => void }) {
  const { current, error, retry } = useSharePreview(workspace.id, {})
  return <Flow title="Review sharing change" cancel="Cancel" onClose={onClose}>
    {error ? <><p role="alert">{error}</p><Action id="send-shares-retry" onClick={retry}>Try again</Action></> :
      <AddRuleReview ws={workspace.id} workspace={workspace} after={current} onDone={onClose} />}
  </Flow>
}

export function UnsentShares({ workspace }: { workspace: Workspace }) {
  const owner = useViewerRole(workspace) === 'owner'
  const share = useQuery({ ...hubQueries.share(workspace.id), enabled: owner && hubServed(workspace.id) })
  const [open, setOpen] = useState(false)
  const n = share.data?.not_sent_count ?? 0
  return <>
    {owner && n > 0 && <div className="flex flex-wrap items-center gap-2" data-testid="unsent-shares">
      <p>{n} shares were made before you joined and are not sent yet.</p>
      <Action id="send-shares-open" onClick={() => setOpen(true)}>Send now</Action>
    </div>}
    {open && <SendSharesReview workspace={workspace} onClose={() => setOpen(false)} />}
  </>
}

/** Mounted only after this join succeeds. Later is deliberately only component state. */
export function JoinShareOffer({ ws, onClose }: { ws: string; onClose: () => void }) {
  const { workspaces } = useWorkspace()
  const workspace = workspaces.find(w => w.id === ws)
  const [review, setReview] = useState(false)
  return <div className="space-y-2" data-testid="join-share-offer">
    <p>Some shares were made before you joined. Send them now?</p>
    <Action id="join-shares-send" disabled={!workspace} onClick={() => setReview(true)}>Send now</Action>
    <Action id="join-shares-later" onClick={onClose}>Later</Action>
    {review && workspace && <SendSharesReview workspace={workspace} onClose={onClose} />}
  </div>
}
