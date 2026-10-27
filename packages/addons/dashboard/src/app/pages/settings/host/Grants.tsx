import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { runnerQuery } from '@/api/agents'
import { canRevokeHostGrant, type HostGrant } from '@/api/settings'
import { clickReady } from '@/api/click'
import { hasFeature } from '@/api/features'
import type { Workspace } from '@/api/types'
import { AgentName } from '@/components/agents/AgentIcon'
import { LoadFailed } from '@/components/LoadFailed'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { fmtDateTime, fmtWhen, nowMs, plural } from '@/lib/time'
import { queries } from '@/api/queries'
import { Raw, visible } from '@/components/sign/visible'
import { RunnerGrantCard } from '@/app/terminal/real/StartAgentDialog'
import { Mono } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { GrantReveal } from './GrantReveal'
import { useViewerPerson, useViewerRole } from './useViewerRole'

/** " (given 9 Oct 11:36)" for the access a follow-up grant came from, when that access is listed; nothing when it is not. */
const parentOf = (lists: HostGrant[], more: HostGrant[], id: string) => {
  const p = [...lists, ...more].find((x) => x.id === id)
  return p ? ` (given ${fmtDateTime(p.issued_at)})` : ''
}

const state = (g: HostGrant, now: number) => (g.revoked ? 'revoked' : Date.parse(g.expires_at) <= now ? 'expired' : 'active')

/** The agents a person can run: both are named wherever their coloured marks appear. */
const AGENTS = ['claude-code', 'codex']

/** The reason is typed before the request exists; the action is rebuilt as it changes, and the hand-off starts on the one button. */
function StopDialog({ ws, grant, name, onClose }: { ws: string; grant: HostGrant; name: string; onClose: () => void }) {
  const [reason, setReason] = useState('')
  const text = reason.trim()
  return (
    <SignPopover
      title={`Stop access for ${visible(name)}`}
      ws={ws}
      action={{ action: 'grant_revoke', grant: grant.id, ...(text ? { reason: text } : {}) }}
      confirmLabel="Stop access"
      onClose={onClose}
    >
      <div className="space-y-1.5">
        <Label htmlFor="revoke-reason">Why (optional)</Label>
        <Input id="revoke-reason" value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} />
        <p className="text-[12px] text-text-faint">Every agent running with this access stops saving changes. One line, at most 200 characters.</p>
      </div>
    </SignPopover>
  )
}

/** "Give agents access": the same sheet as the pill in the top bar ("Let agents work"). */
function GiveAccess({ ws }: { ws: string }) {
  const [open, setOpen] = useState(false)
  const qc = useQueryClient()
  const runner = useQuery({ ...runnerQuery(ws), enabled: hasFeature('agent_start', ws), retry: false })
  const refresh = () => {
    void runner.refetch()
    void qc.invalidateQueries({ queryKey: ['settings-grants', ws] })
  }
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>Give agents access</Button>
      {open && (
        <Dialog open onOpenChange={(o) => !o && setOpen(false)}>
          <DialogContent className="max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Allow agent access</DialogTitle>
              <DialogDescription>Choose how long agents may work for you, counted from now.</DialogDescription>
            </DialogHeader>
            <RunnerGrantCard sheet ws={ws} runner={runner.data?.runner ?? null} loading={runner.isPending} blocked={null} onChanged={refresh} onOutcome={refresh} onConfirmed={() => setOpen(false)} />
          </DialogContent>
        </Dialog>
      )}
    </>
  )
}

/** #1198 C7: access for a tool outside the dashboard. Its secret is shown once in this page, sealed to a key made here. */
function GiveToolAccess({ workspace, onChanged }: { workspace: Workspace; onChanged: () => void }) {
  const [step, setStep] = useState<'closed' | 'form' | 'run'>('closed')
  const [hours, setHours] = useState(24)
  const [label, setLabel] = useState('')
  const name = label.trim()
  const [busy, setBusy] = useState(false)
  if (!clickReady(workspace)) return null
  return (
    <>
      <Button variant="outline" data-testid="give-tool-access" onClick={() => setStep('form')}>Give a tool access</Button>
      {step !== 'closed' && (
        <Dialog open onOpenChange={(o) => !o && !busy && setStep('closed')}>
          <DialogContent className="max-h-[90vh] overflow-y-auto">
            <DialogHeader>
              <DialogTitle>Give a tool access</DialogTitle>
              <DialogDescription>For a tool that runs outside this dashboard. You get a secret to give it, shown once.</DialogDescription>
            </DialogHeader>
            {step === 'form' ? (
              <div className="space-y-4">
                <div role="radiogroup" aria-label="Access for" className="grid grid-cols-3 gap-2">
                  {([[24, '1 day'], [72, '3 days'], [168, '7 days']] as const).map(([h, text]) => (
                    <button key={h} type="button" role="radio" aria-checked={h === hours} onClick={() => setHours(h)} className={`min-h-11 rounded-lg border px-2 py-2 text-sm font-medium ${h === hours ? 'border-warning bg-warning/10 text-warning' : 'border-border bg-surface text-text hover:bg-bg'}`}>{text}</button>
                  ))}
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="tool-name">Name of the tool (optional)</Label>
                  <Input id="tool-name" value={label} maxLength={40} onChange={(e) => setLabel(e.target.value)} />
                </div>
                <p className="text-[12px] text-text-muted">Anyone who holds the secret can act for you on tickets you may work on until the access ends. You can stop it at any time.</p>
                <Button className="w-full" onClick={() => setStep('run')}>Continue to the review</Button>
              </div>
            ) : (
              <GrantReveal ws={workspace.id} hours={hours} scope="workable" label={name || undefined} onChanged={onChanged} onBusy={setBusy} onClose={() => { setBusy(false); setStep('closed') }} />
            )}
          </DialogContent>
        </Dialog>
      )}
    </>
  )
}

/** Agent access as the host gives it to this person: an owner sees everyone's, anyone else their own. Stopping is one step: a reason (optional) and one sign button. */
export function HostGrants({ workspace }: { workspace: Workspace }) {
  const [ended, setEnded] = useState(false)
  const grants = useQuery(queries.hostGrants(workspace.id, !ended))
  const roster = useQuery(queries.roster(workspace.id))
  const role = useViewerRole(workspace)
  const viewer = useViewerPerson()
  const [stopping, setStopping] = useState<HostGrant | null>(null)
  // UP-2: the runner on this machine is authoritative for the viewer's own runner access (the pill reads it too).
  const runner = useQuery({ ...runnerQuery(workspace.id), enabled: hasFeature('agent_start', workspace.id), retry: false })
  const heldId = runner.isSuccess ? (runner.data?.runner?.grant?.id ?? null) : undefined
  if (grants.isError) return <LoadFailed what="agent access" onRetry={() => void grants.refetch()} />
  const name = (id: string) => roster.data?.find((m) => m.person === id)?.name ?? workspace.members.find((m) => m.person === id)?.name ?? id
  const served = hasFeature('do:grant.revoke', workspace.id)
  const now = nowMs()
  const rows = grants.data ?? []
  const active = rows.filter((g) => state(g, now) === 'active')
  const past = rows.filter((g) => state(g, now) !== 'active')
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-base font-semibold">Agent access</h2>
        <GiveToolAccess workspace={workspace} onChanged={() => void grants.refetch()} />
      </div>
      <p className="text-[13px] text-text-muted">
        {role === 'owner' ? 'Who has let agents work in this workspace, and until when.' : 'The access you have given to your agents.'}
      </p>
      {!grants.data ? (
        <p className="text-[13px] text-text-muted">Reading the agent access…</p>
      ) : (
        <>
          {active.length === 0 && (
            <div data-testid="no-grants" className="space-y-3 rounded-lg border border-border bg-surface p-4">
              <p className="text-[14px]">Nobody has given agents access.</p>
              <GiveAccess ws={workspace.id} />
            </div>
          )}
          {active.map((g) => {
            // Active on paper, but the agent runner on this computer does not hold it (an update restarts the runner and it loses the secret): agents cannot use it.
            const orphan = g.label === 'runner' && g.person === viewer && heldId !== undefined && heldId !== g.id
            const who = visible(name(g.person))
            return (
              <article key={g.id} data-testid="grant-card" aria-label={`${who}’s agents`} className="space-y-3 rounded-lg border border-border bg-surface p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-[14px] font-medium"><bdi>{who}</bdi>’s agents</h3>
                  <span className={orphan ? 'rounded-full border border-border px-2 py-px text-[11px] text-text-muted' : 'rounded-full border border-brand/40 px-2 py-px text-[11px] text-brand'}>{orphan ? 'Not in use' : 'Active'}</span>
                  {g.label === 'runner' && <span data-testid="runner-grant-tag" className="text-[11px] text-text-faint">on this computer</span>}
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {AGENTS.map((a) => <AgentName key={a} harness={a} />)}
                </div>
                {g.parent ? (
                  <p className="text-[13px]" data-testid="grant-parent">
                    A follow-up run: one session on one ticket, with the rights of the access it came from{parentOf(active, past, g.parent)}. It ends when the run ends.
                  </p>
                ) : (
                  <p className="text-[13px]">Can work on tickets they are allowed to.</p>
                )}
                <p className="text-[13px] tabular-nums text-text-muted">
                  Agents can work until {fmtDateTime(g.expires_at)}{g.sessions > 0 ? ` · ${plural(g.sessions, 'agent')} running` : ''}
                </p>
                {orphan && <p data-testid="grant-not-held" className="text-[12px] text-text-muted">The agent runner on this computer no longer holds this access (an update resets it), so agents cannot use it. Give access again from the top bar, or stop this one.</p>}
                {canRevokeHostGrant(role, g.person, viewer) && (
                  <Button variant="outline" size="sm" className="border-danger/50 text-danger hover:text-danger" disabled={!served} title={served ? undefined : 'This host cannot stop access yet.'} aria-label={`Stop access for ${who}`} onClick={() => setStopping(g)}>
                    Stop access
                  </Button>
                )}
                <details className="text-[12px] text-text-faint">
                  <summary className="cursor-pointer text-text-muted">Details</summary>
                  <div className="mt-1 space-y-0.5">
                    <p>What it covers: <Mono className="text-[11px]"><Raw>{g.scope}</Raw></Mono></p>
                    {g.label && g.label !== 'runner' && <p>Label <Raw>{g.label}</Raw></p>}
                    <p className="break-all">Access id <Mono className="text-[11px]">{g.id}</Mono></p>
                    {g.session_ids && g.session_ids.length > 0 && <p className="break-all">Sessions <Mono className="text-[11px]"><Raw>{g.session_ids.join(', ')}</Raw>{g.sessions_more ? ', …' : ''}</Mono></p>}
                  </div>
                </details>
              </article>
            )
          })}
          <label className="flex items-center gap-2 text-[13px] text-text-muted">
            <Switch checked={ended} onCheckedChange={setEnded} aria-label="Show past access" />
            Show past access
          </label>
          {ended && (
            <ul aria-label="Past access" className="space-y-2">
              {past.length === 0 && <li className="text-[13px] text-text-muted">No past access.</li>}
              {past.map((g) => (
                <li key={g.id} className="rounded-lg border border-border px-3 py-2 text-[13px] text-text-muted">
                  <span className="text-text"><Raw>{name(g.person)}</Raw>’s agents</span> ·{' '}
                  {g.revoked ? <>Stopped {fmtWhen(g.revoked.at)}{g.revoked.by ? <> by <Raw>{name(g.revoked.by)}</Raw></> : ''}</> : <>Ended {fmtWhen(g.expires_at)}</>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      {role !== 'owner' && <OwnerNote>Owners see and stop everyone’s access; everyone else their own.</OwnerNote>}
      {stopping && <StopDialog ws={workspace.id} grant={stopping} name={name(stopping.person)} onClose={() => setStopping(null)} />}
    </div>
  )
}
