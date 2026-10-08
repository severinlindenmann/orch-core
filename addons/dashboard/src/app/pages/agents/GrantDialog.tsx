import { useQueryClient } from '@tanstack/react-query'
import { Fingerprint, ShieldCheck } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, type GrantInfo } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'

const TOUCH_ID_MS = 600

export type GrantAction = { kind: 'issue' } | { kind: 'revoke'; grant: GrantInfo }

/**
 * Signs an issue or revoke (Touch ID simulation). The prompt closes on click and progress goes to a toast:
 * the modal dialog hides the page for assistive tech while it is open, and the result belongs on the page.
 */
export function useSignGrant(ws: string) {
  const qc = useQueryClient()
  return async (action: GrantAction, hours: number) => {
    const title = action.kind === 'issue' ? 'Issue a grant' : `Revoke ${action.grant.id}`
    const id = toast.loading('Touch the sensor to confirm')
    await new Promise((r) => setTimeout(r, TOUCH_ID_MS))
    try {
      if (action.kind === 'issue') await api.issueGrant(ws, { hours, scope: 'all' })
      else await api.revokeGrant(ws, action.grant.id)
      await qc.invalidateQueries()
      toast.success(`${title}: signed with Touch ID`, { id })
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Could not sign', { id })
    }
  }
}

/** Core-rendered signing prompt for issuing and revoking grants (SignDialog is ticket-bound). */
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
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg gap-4 border-border bg-surface">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-brand" />
            {title}
          </DialogTitle>
          <DialogDescription>Grants are signed by a person with their own key. Only core shows this prompt; an agent or an addon cannot sign it.</DialogDescription>
        </DialogHeader>

        {issue && (
          <div className="space-y-1.5">
            <Label htmlFor="grant-hours">Hours: {hours}</Label>
            <input id="grant-hours" type="range" min={1} max={12} step={1} value={hours} onChange={(e) => setHours(Number(e.target.value))} className="w-full accent-[var(--brand)]" />
          </div>
        )}

        <dl className="grid grid-cols-[88px_1fr] gap-x-3 rounded-md border border-border bg-bg p-3 text-[13px]">
          <dt className="text-text-muted">Covers</dt>
          <dd>
            <ul className="list-disc space-y-0.5 pl-4">
              {covers.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </dd>
        </dl>

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={issue ? 'default' : 'destructive'} onClick={() => onSign(action, hours)}>
            <Fingerprint />
            Sign with Touch ID
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
