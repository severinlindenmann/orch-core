import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import type { GrantInfo } from '@/api/types'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'
import { Label } from '@/components/ui/label'

export type GrantAction = { kind: 'issue' } | { kind: 'revoke'; grant: GrantInfo }

/** Signs an issue or revoke with the shared Touch ID simulation; progress and result go to a toast. */
export function useSignGrant(ws: string) {
  const signed = useSignedAction()
  return async (action: GrantAction, hours: number) => {
    const title = action.kind === 'issue' ? 'Issue a grant' : `Revoke ${action.grant.id}`
    await signed(title, () => (action.kind === 'issue' ? api.issueGrant(ws, { hours, scope: 'all' }) : api.revokeGrant(ws, action.grant.id)))
  }
}

/** Issue or revoke prompt (SignDialog is ticket-bound; SignPrompt is the shared, ticket-independent primitive). */
export function GrantDialog({ action, now, onSign, onClose }: { action: GrantAction | null; now: string; onSign: (a: GrantAction, hours: number) => void; onClose: () => void }) {
  const [hours, setHours] = useState(8)

  useEffect(() => setHours(8), [action])

  if (!action) return null
  const issue = action.kind === 'issue'
  const until = new Date(Date.parse(now) + hours * 3600_000).toISOString().slice(11, 16)
  const title = issue ? 'Issue a grant' : `Revoke ${action.grant.id}`
  const covers = issue
    ? ['Scope: all tickets in this workspace', `Duration: ${hours} h, until ${until} UTC`, 'Agents never enable addons and never get pty']
    : [
        `Grant ${action.grant.id} for ${action.grant.person}`,
        action.grant.sessions.length ? `Stops ${action.grant.sessions.length} session${action.grant.sessions.length === 1 ? '' : 's'}: ${action.grant.sessions.join(', ')}` : 'No session uses it',
        'Releases their claims and task leases (reason: grant revoked)',
      ]

  return (
    <SignPrompt title={title} covers={covers} destructive={!issue} onSign={() => onSign(action, hours)} onClose={onClose}>
      {issue && (
        <div className="space-y-1.5">
          <Label htmlFor="grant-hours">Hours: {hours}</Label>
          <input id="grant-hours" type="range" min={1} max={12} step={1} value={hours} onChange={(e) => setHours(Number(e.target.value))} className="w-full accent-[var(--brand)]" />
        </div>
      )}
    </SignPrompt>
  )
}
