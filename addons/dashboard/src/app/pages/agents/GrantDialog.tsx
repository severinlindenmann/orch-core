import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import type { GrantInfo } from '@/api/types'
import { grantLabel, grantTerms, scopeCover } from '@/api/grants'
import { useRole } from '@/app/useRole'
import { fmtExact } from '@/lib/time'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'
import { Label } from '@/components/ui/label'
import { useWorkspace } from '../../workspace'

export type GrantAction = { kind: 'issue' } | { kind: 'revoke'; grant: GrantInfo }

/** Signs an issue or revoke with the shared Touch ID simulation; progress and result go to a toast. */
export function useSignGrant(ws: string) {
  const signed = useSignedAction()
  const { workspace } = useWorkspace()
  const terms = grantTerms(useRole(), workspace)
  const name = (id: string) => workspace?.members.find((m) => m.person === id)?.name ?? id
  return async (action: GrantAction, hours: number) => {
    const title = action.kind === 'issue' ? 'Issue a grant' : `Revoke ${grantLabel(action.grant, name(action.grant.person))}`
    await signed(title, () => (action.kind === 'issue' ? api.issueGrant(ws, { hours, scope: terms?.scope ?? 'workable' }) : api.revokeGrant(ws, action.grant.id)))
  }
}

/** Issue or revoke prompt (SignDialog is ticket-bound; SignPrompt is the shared, ticket-independent primitive). */
export function GrantDialog({ action, now, onSign, onClose }: { action: GrantAction | null; now: string; onSign: (a: GrantAction, hours: number) => void; onClose: () => void }) {
  const { workspace } = useWorkspace()
  // Owners and maintainers: all tickets, up to 24 h. Members: the tickets they may work on, up to the workspace default.
  const terms = grantTerms(useRole(), workspace) ?? { scope: 'workable' as const, maxHours: 0, defaultHours: 0 }
  const [hours, setHours] = useState(terms.defaultHours)
  const ws = workspace?.id
  const sessions = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws && action?.kind === 'revoke' })
  const person = (id: string) => (workspace ? (workspace.members.find((m) => m.person === id)?.name ?? id) : '…')

  useEffect(() => setHours(terms.defaultHours), [action, terms.defaultHours])

  if (!action) return null
  const issue = action.kind === 'issue'
  // What is signed is an instant: the covers say it exactly, with its time zone (the table may stay short).
  const until = fmtExact(new Date(Date.parse(now) + hours * 3600_000).toISOString())
  const sessionName = (id: string) => {
    const s = sessions.data?.find((x) => x.session === id)
    return s ? `${s.name} for ${person(s.for)}` : id
  }
  const sessionList = (ids: string[]) => {
    const n = new Map<string, number>()
    for (const id of ids) n.set(sessionName(id), (n.get(sessionName(id)) ?? 0) + 1)
    return [...n].map(([label, c]) => (c > 1 ? `${label} (${c})` : label)).join(', ')
  }
  const label = issue ? '' : grantLabel(action.grant, person(action.grant.person), Date.parse(now))
  const title = issue ? 'Issue a grant' : `Revoke ${label}`
  const covers = issue
    ? [`Scope: ${scopeCover(terms.scope)}`, `Duration: ${hours} h, until ${until}`, 'For you: your agents act in your name, signed with your key']
    : [
        `${label} · ${action.grant.id}`,
        action.grant.sessions.length ? `Stops ${action.grant.sessions.length} session${action.grant.sessions.length === 1 ? '' : 's'}: ${sessionList(action.grant.sessions)}` : 'No session uses it',
        'Releases their claims and task leases (reason: grant revoked)',
      ]

  return (
    <SignPrompt title={title} covers={covers} destructive={!issue} confirmLabel={issue ? 'Issue grant' : 'Revoke grant'} onSign={() => onSign(action, hours)} onClose={onClose}>
      {issue && (
        <div className="space-y-1.5">
          <Label htmlFor="grant-hours">Hours: {hours}</Label>
          <input id="grant-hours" type="range" min={1} max={terms.maxHours} step={1} value={hours} onChange={(e) => setHours(Number(e.target.value))} className="w-full accent-[var(--brand)]" />
          {terms.scope === 'workable' && <p className="text-[12px] text-text-muted">As a member you grant yourself at most {terms.maxHours} h (the workspace default), for the tickets you may work on. An owner can revoke it at any time.</p>}
        </div>
      )}
    </SignPrompt>
  )
}
