import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { hubQueries } from '@/api/hub'
import { Raw } from '@/components/sign/visible'

/** Who should see what is shared (hub ids; nothing ticked = only me) and whether the description goes along. The names
 * come from the hub, not signed; the review that follows (from the host) names the same people again before anything is sent. */
export function AudiencePicker({ ws, enabled = true, users, groups, withText, onUsers, onGroups, onWithText }: {
  ws: string; enabled?: boolean; users: string[]; groups: string[]; withText: boolean
  onUsers: (f: (u: string[]) => string[]) => void; onGroups: (f: (g: string[]) => string[]) => void; onWithText: (v: boolean) => void
}) {
  const audience = useQuery({ ...hubQueries.audience(ws), enabled })
  // Exactly one other person and no team: that person is ticked once (the review that follows names them again).
  const seeded = useRef(false)
  const only = audience.data && audience.data.people.length === 1 && audience.data.teams.length === 0 ? audience.data.people[0].user_id : null
  useEffect(() => {
    if (seeded.current || !only) return
    seeded.current = true
    onUsers(u => (u.length ? u : [only]))
  }, [only, onUsers])
  return <>
    <fieldset className="space-y-1" data-testid="hub-rule-audience"><legend>Who should see it?</legend>
      {audience.isPending ? <p role="status">Loading people and teams…</p> : audience.isError ? <p role="alert">Could not load people and teams. You can still share with only yourself.</p> : <>
        {audience.data.people.map(p => <label key={p.user_id} className="flex items-center gap-2"><input type="checkbox" data-testid={'hub-rule-person-' + p.user_id} checked={users.includes(p.user_id)} onChange={e => onUsers(u => e.target.checked ? [...u, p.user_id] : u.filter(x => x !== p.user_id))} /><Raw>{p.name}</Raw></label>)}
        {audience.data.teams.map(t => <label key={t.group} className="flex items-center gap-2"><input type="checkbox" data-testid={'hub-rule-team-' + t.group} checked={groups.includes(t.group)} onChange={e => onGroups(g => e.target.checked ? [...g, t.group] : g.filter(x => x !== t.group))} />Team: <Raw>{t.group}</Raw> <span className="text-xs text-text-muted">{t.members === 1 ? '1 person' : t.members + ' people'}</span></label>)}
        <p className="text-xs text-text-muted" data-testid="hub-rule-only-me">{users.length || groups.length ? 'Names from the hub, not signed.' : 'Nobody else sees it until you pick someone.'}</p>
      </>}
    </fieldset>
    <label className="flex items-start gap-2"><input type="checkbox" data-testid="hub-rule-description" checked={withText} onChange={e => onWithText(e.target.checked)} /><span>Include the description <span className="block text-xs text-text-muted">Comments and files stay private.</span></span></label>
  </>
}
