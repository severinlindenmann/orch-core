import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Action, Flow } from '../settings/hub/shared'
import { AddRuleReview } from '../settings/hub/AddRule'
import { AudiencePicker } from '../settings/hub/AudiencePicker'
import { useSharePreview } from '../settings/hub/useSharePreview'
import type { HubWorkspace } from '../settings/hub/StopSharing'
import { hubQueries, type HubAudience, type HubRule } from '@/api/hub'

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
  // B13: with nobody else on the hub there is no one to share with; Share stays off and says what to do first.
  const audience = useQuery(hubQueries.audience(ws))
  const alone = !!audience.data && audience.data.people.length === 0 && audience.data.teams.length === 0
  if (alone) return <Flow title="Share this ticket" onClose={onClose} finish="here">
    <div className="space-y-2" data-testid="hub-add-review">
      <p role="status" data-testid="hub-add-blocked">Nobody else is on your hub yet. Invite someone first.</p>
      <div className="flex items-center justify-end gap-2">
        <Button data-testid="hub-add-cancel" variant="outline" size="sm" onClick={onClose}>Cancel</Button>
        <Button data-testid="hub-add-go" size="sm" disabled>Share</Button>
      </div>
    </div>
  </Flow>
  return <Flow title="Share this ticket" onClose={onClose} finish="here">
    <div className="space-y-3"><AudiencePicker ws={ws} compact users={users} groups={groups} withText={withText} onUsers={setUsers} onGroups={setGroups} onWithText={setWithText} /></div>
    {error
      ? <div role="alert"><p className="text-danger">{error}</p><Action id="hub-preview-retry" onClick={retry}>Try again</Action></div>
      : <AddRuleReview ws={ws} workspace={workspace} rule={rule} title={title} compact after={current} onDone={onClose} onCancel={onClose} />}
  </Flow>
}
