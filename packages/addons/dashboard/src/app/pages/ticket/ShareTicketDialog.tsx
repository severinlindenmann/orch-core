import { useState } from 'react'
import { Action, Flow } from '../settings/hub/shared'
import { AddRuleReview } from '../settings/hub/AddRule'
import { AudiencePicker } from '../settings/hub/AudiencePicker'
import { useSharePreview } from '../settings/hub/useSharePreview'
import type { HubWorkspace } from '../settings/hub/StopSharing'
import type { HubAudience, HubRule } from '@/api/hub'

/**
 * Share ONE ticket with the hub from the ticket itself. It is the Settings path (`share.rule_add` for a `ticket` rule):
 * the same preview, the same host review (who will see it, the description, what reaches more people now) and the same
 * plan bound to the confirm. Nothing is typed and nothing here words the review: `AddRuleReview` shows the host's text.
 */
export function ShareTicketDialog({ ws, workspace, ticket, title, onClose }: { ws: string; workspace: HubWorkspace; ticket: string; title: string; onClose: () => void }) {
  const [users, setUsers] = useState<string[]>([])
  const [groups, setGroups] = useState<string[]>([])
  const [withText, setWithText] = useState(true)
  const readers: HubAudience | undefined = users.length || groups.length ? { users: [...users].sort(), groups: [...groups].sort() } : undefined
  const rule: HubRule = { kind: 'ticket', value: ticket, ...(readers ? { audience: readers } : {}), description: withText }
  const { current, error, retry } = useSharePreview(ws, { add: rule })
  return <Flow title="Share this ticket" onClose={onClose}>
    <div className="space-y-3"><AudiencePicker ws={ws} compact users={users} groups={groups} withText={withText} onUsers={setUsers} onGroups={setGroups} onWithText={setWithText} /></div>
    {error
      ? <div role="alert"><p className="text-danger">{error}</p><Action id="hub-preview-retry" onClick={retry}>Try again</Action></div>
      : <AddRuleReview ws={ws} workspace={workspace} rule={rule} title={title} compact after={current} onDone={onClose} />}
  </Flow>
}
