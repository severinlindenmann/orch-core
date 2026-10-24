import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { runnerQuery } from '@/api/agents'
import { canRevokeHostGrant, type HostGrant } from '@/api/settings'
import { hasFeature } from '@/api/features'
import type { Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { fmtDateTime, nowMs } from '@/lib/time'
import { queries } from '@/api/queries'
import { Raw, visible } from '@/components/sign/visible'
import { Mono, Pill } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useViewerPerson, useViewerRole } from './useViewerRole'

const state = (g: HostGrant, now: number) => (g.revoked ? 'revoked' : Date.parse(g.expires_at) <= now ? 'expired' : 'active')
const TONE = { active: 'brand', expired: 'neutral', revoked: 'danger' } as const

/** The reason is typed before the request exists; the action is rebuilt as it changes, and the hand-off starts on the button. */
function RevokeDialog({ ws, grant, name, onClose }: { ws: string; grant: HostGrant; name: string; onClose: () => void }) {
  const [reason, setReason] = useState('')
  const text = reason.trim()
  return (
    <SignPopover
      title={`Revoke the grant of ${visible(name)}`}
      ws={ws}
      action={{ action: 'grant_revoke', grant: grant.id, ...(text ? { reason: text } : {}) }}
      confirmLabel="Revoke grant"
      onClose={onClose}
    >
      <div className="space-y-1.5">
        <Label htmlFor="revoke-reason">Why (optional)</Label>
        <Input id="revoke-reason" value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} />
        <p className="text-[12px] text-text-faint">
          One line, at most 200 characters. Every agent session using this grant ends. 
        </p>
      </div>
    </SignPopover>
  )
}

/** Agent grants as the host gives this person: an owner sees every grant, anyone else their own. Revoking is a hand-off. */
export function HostGrants({ workspace }: { workspace: Workspace }) {
  const [ended, setEnded] = useState(false)
  const grants = useQuery(queries.hostGrants(workspace.id, !ended))
  const roster = useQuery(queries.roster(workspace.id))
  const role = useViewerRole(workspace)
  const viewer = useViewerPerson()
  const [revoking, setRevoking] = useState<HostGrant | null>(null)
  // UP-2: the runner on this machine is authoritative for the viewer's own runner access (the pill reads it too).
  const runner = useQuery({ ...runnerQuery(workspace.id), enabled: hasFeature('agent_start', workspace.id), retry: false })
  const heldId = runner.isSuccess ? (runner.data?.runner?.grant?.id ?? null) : undefined
  if (grants.isError) return <LoadFailed what="grants" onRetry={() => void grants.refetch()} />
  const name = (id: string) => roster.data?.find((m) => m.person === id)?.name ?? workspace.members.find((m) => m.person === id)?.name ?? id
  const served = hasFeature('do:grant.revoke', workspace.id)
  const now = nowMs()
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="flex-1 text-base font-semibold">Agent grants</h2>
        <label className="flex items-center gap-2 text-[13px] text-text-muted">
          <input type="checkbox" checked={ended} onChange={(e) => setEnded(e.target.checked)} className="accent-[var(--brand)]" />
          Show ended grants
        </label>
      </div>
      <p className="text-[13px] text-text-muted">
        {role === 'owner' ? 'Every grant of the workspace.' : 'Your own grants.'} Agent access is given from the “Agents can work for you” pill at the top (Extend or Start again) or in your terminal; here you can revoke a grant.
      </p>
      {!grants.data ? (
        <p className="text-[13px] text-text-muted">Reading the grants…</p>
      ) : grants.data.length === 0 ? (
        <p data-testid="no-grants" className="rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-text-muted">
          {ended ? 'No grants.' : 'No active grants.'}
        </p>
      ) : (
        <div className="rounded-lg border border-border bg-surface">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Grant</TableHead>
                <TableHead>Scope</TableHead>
                <TableHead>Until</TableHead>
                <TableHead>Sessions</TableHead>
                <TableHead>State</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {grants.data.map((g) => {
                const s = state(g, now)
                // Active on paper, but the agent runner on this Mac does not hold it (an update restarts the runner and it loses the secret): agents cannot use it.
                const orphan = s === 'active' && g.label === 'runner' && g.person === viewer && heldId !== undefined && heldId !== g.id
                return (
                  <TableRow key={g.id}>
                    <TableCell>
                      <span className="block"><Raw>{name(g.person)}</Raw></span>
                      <span className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-text-faint">
                        {g.label === 'runner' ? (
                          <span data-testid="runner-grant-tag"><Pill tone="neutral">runner grant</Pill></span>
                        ) : g.label ? (
                          <span>label <Raw>{g.label}</Raw></span>
                        ) : null}
                      </span>
                      <details className="text-[11px] text-text-faint">
                        <summary className="cursor-pointer">Technical details</summary>
                        <span className="block break-all">Grant <Mono className="text-[11px] text-text-faint">{g.id}</Mono></span>
                        {g.session_ids && g.session_ids.length > 0 && <span className="block break-all">Sessions <Mono className="text-[11px] text-text-faint"><Raw>{g.session_ids.join(', ')}</Raw>{g.sessions_more ? ', …' : ''}</Mono></span>}
                      </details>
                    </TableCell>
                    <TableCell>
                      <Mono><Raw>{g.scope}</Raw></Mono>
                    </TableCell>
                    <TableCell className="whitespace-normal tabular-nums">
                      {g.revoked ? (
                        <span className="text-text-muted">
                          Revoked {fmtDateTime(g.revoked.at)}
                          {g.revoked.by ? <> by <Raw>{name(g.revoked.by)}</Raw></> : ''}
                        </span>
                      ) : (
                        fmtDateTime(g.expires_at)
                      )}
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {g.sessions}
                    </TableCell>
                    <TableCell>
                      <Pill tone={orphan ? 'neutral' : TONE[s]} className="capitalize">
                        {orphan ? 'Not in use' : s}
                      </Pill>
                      {orphan && <span data-testid="grant-not-held" className="mt-1 block text-[11px] text-text-faint">The agent runner on this Mac no longer holds this access (an update resets it), so agents cannot use it. Give access again from the pill at the top; you can revoke this one.</span>}
                    </TableCell>
                    <TableCell className="text-right">
                      {s === 'active' && canRevokeHostGrant(role, g.person, viewer) && (
                        <Button variant="outline" size="xs" className="text-danger hover:text-danger" disabled={!served} title={served ? undefined : 'This host cannot revoke grants yet.'} aria-label={`Revoke the grant of ${visible(name(g.person))}`} onClick={() => setRevoking(g)}>
                          Revoke
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}
      {role !== 'owner' && <OwnerNote>Owners see and revoke every grant; everyone else their own.</OwnerNote>}
      {revoking && <RevokeDialog ws={workspace.id} grant={revoking} name={name(revoking.person)} onClose={() => setRevoking(null)} />}
    </div>
  )
}
