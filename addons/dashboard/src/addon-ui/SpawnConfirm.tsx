import { useQuery } from '@tanstack/react-query'
import { Bot, TriangleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { api } from '@/api/client'
import { activeGrantOf } from '@/api/grants'
import { can } from '@/api/permissions'
import type { CoreLaunch, LaunchPreview, TicketDocument } from '@/api/types'
import { blockedText, type TicketNeeds } from '@/api/connections'
import { agentName } from '@/app/pages/ticket/shared'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { AddonBadge } from './AddonBadge'
import { canSpawnAgent } from './capabilities'
import { addonStateKey, useAddons } from './slots'

/** Hours of the grant a person signs here when they have none. */
const GRANT_HOURS = 8
/** Longest addon-supplied text shown in the dialog. */
const ADDON_TEXT_MAX = 300
const hhmm = (iso: string) => `${iso.slice(11, 16)} UTC`
const cap = (v: unknown) => {
  const t = typeof v === 'string' ? v : ''
  return t.length > ADDON_TEXT_MAX ? `${t.slice(0, ADDON_TEXT_MAX)}…` : t
}

/**
 * Core's precheck before any start dialog: a ticket an agent already holds cannot get a second one. Computed from the
 * ticket document (core), never from an addon. Null when nothing stands in the way.
 */
export function claimedReason(ticket: { key: string; claim: { agent: string; for: string } | null }, name: (id: string) => string): string | null {
  const c = ticket.claim
  if (!c) return null
  return `${ticket.key} is claimed by ${agentName(c.agent)} for ${name(c.for)}. Stop that session on Agents first.`
}

/**
 * Core's second precheck (D57): a ticket that needs a connection whose last check is auth expired or wrong identity
 * gets no agent until an owner logs in again. Read from the ticket document's `needs` (core-computed), never an addon.
 */
export function connectionReason(ticket: { needs?: TicketNeeds }): string | null {
  const b = ticket.needs?.blocked
  return b ? `${blockedText(b)}. An owner logs in again first (Today or Settings > Connections); agents never handle logins.` : null
}

/** Both of core's prechecks, the claim first. */
export const precheckReason = (ticket: Parameters<typeof claimedReason>[0] & { needs?: TicketNeeds }, name: (id: string) => string) => claimedReason(ticket, name) ?? connectionReason(ticket)

/** Core's warning when gates before the work are open: the run begins with them. */
function gateWarning(t: TicketDocument): string | null {
  const req = t.gates.requirements.state !== 'approved'
  const plan = t.gates.plan.state !== 'approved'
  if (req && plan) return 'Requirements and plan are not approved yet. The agent starts by refining them.'
  if (req) return 'Requirements are not approved yet. The agent starts by refining them.'
  if (plan) return 'The plan is not approved yet. The agent starts by refining it.'
  return null
}

/** What core starts after the person confirms: the ticket and the validated choice, as core computed them. */
export interface ConfirmedLaunch {
  ticket: string
  mode: string
  harness: string
  where: string
}

/**
 * Core's confirmation before an addon's `spawn_agent` action runs (never an addon node).
 *
 * Trust split: the addon only says WHICH ticket and choice it asks for (ids from its state). Every fact shown as
 * core's — workspace, ticket key and title, mode, harness, where, model, the exact command, the grant — comes from
 * core (`api.previewLaunch`, the ticket read through the API, the grants list). Anything the addon displayed that
 * differs is shown apart, under "From addon <name>", as capped plain text. The confirm label is core's. The action
 * is then posted with the values core showed (`onStart`), and the host validates them again.
 *
 * With an active grant it is a plain confirmation; without one, an owner or maintainer signs a grant here first
 * (SignPrompt, Touch ID); a member is told who can issue one.
 */
export function SpawnConfirm({ addon, ticketKey, onStart, onClose }: { addon: string; ticketKey?: string; onStart: (l: ConfirmedLaunch) => void; onClose: () => void }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const signed = useSignedAction()
  const { data: addons } = useAddons()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const grants = useQuery({ queryKey: ['grants', ws], queryFn: () => api.listGrants(ws!), enabled: !!ws })
  // The same request as the ticket panel (per ticket), or the page's (the ticket picked there).
  const state = useQuery({ queryKey: addonStateKey(ws, addon, ticketKey), queryFn: () => api.getAddonState(ws!, addon, ticketKey), enabled: !!ws, retry: false })

  // The request, from the addon (untrusted): a ticket key and three ids. Core validates them below.
  const previews = (state.data?.previews ?? {}) as Record<string, LaunchPreview>
  const selected = state.data?.selected
  const key = ticketKey ?? (typeof selected === 'string' ? selected : undefined)
  const shown = key && Object.hasOwn(previews, key) ? previews[key] : undefined
  const asked = shown?.request
  const request = key && asked ? { ticket: key, mode: String(asked.mode), harness: String(asked.harness), where: String(asked.where) } : undefined

  const core = useQuery({
    queryKey: ['launch-preview', ws, request],
    queryFn: () => api.previewLaunch(ws!, request!),
    enabled: !!ws && !!request,
    retry: false,
  })
  // The ticket as core reads it (the same query as the ticket page): the claim precheck and the gate warning.
  const doc = useQuery({ queryKey: ['ticket', request?.ticket], queryFn: () => api.getTicket(request!.ticket), enabled: !!request, retry: false })
  const allowed = canSpawnAgent(addons?.find((a) => a.name === addon), workspace?.addons[addon])

  const plain = (title: string, text: string) => (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg border-border bg-surface">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{text}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )

  if (!allowed) return plain('Start agent', 'This addon may not start agents in this workspace.')
  if (state.isSuccess && !request) return plain('Start agent', 'Pick a ticket first.')
  if (core.isError || doc.isError) return plain('Start agent', 'orch cannot start what this addon asked for (unknown ticket, mode, harness or place).')
  const claimed = doc.data ? precheckReason(doc.data, (id) => workspace?.members.find((m) => m.person === id)?.name ?? id) : null
  if (claimed) return plain('Start agent', claimed)
  if (!ws || !me.data || !today.data || !grants.data || !core.data || !doc.data) {
    return (
      <Dialog open onOpenChange={(o) => !o && onClose()}>
        <DialogContent className="max-w-lg border-border bg-surface" aria-busy="true">
          <DialogHeader>
            <DialogTitle>Start agent</DialogTitle>
            <DialogDescription>Checking what would start…</DialogDescription>
          </DialogHeader>
        </DialogContent>
      </Dialog>
    )
  }

  const c: CoreLaunch = core.data
  const launch: ConfirmedLaunch = request!
  const now = today.data.now
  const grant = activeGrantOf(grants.data, me.data.person, Date.parse(now))
  const canIssue = can(role, 'grant.issue')
  const until = new Date(Date.parse(now) + GRANT_HOURS * 3600_000).toISOString()
  const facts: [string, string][] = [
    ['Workspace', c.workspace],
    ['Ticket', `${c.ticket} · ${c.title}`],
    ['Mode', c.mode],
    ['Harness', c.harness],
    ['Where', c.where],
    // Rendered by core from the validated plan, never from the addon's text.
    ['Model', `${c.model ? `${c.model}${c.tier ? ` (${c.tier} tier)` : ''}` : 'the harness default'}${c.subagent_model ? `; subagents on ${c.subagent_model}` : ''}`],
    ['Grant', grant ? `active until ${hhmm(grant.until)}; revoking it stops this run` : `none yet: signing issues you one for all tickets here, ${GRANT_HOURS} h, until ${hhmm(until)}`],
  ]
  const warning = gateWarning(doc.data)
  // What the addon displayed, where it differs from what orch will start.
  const differs = shown && (shown.command !== c.command || shown.title !== c.title || shown.mode !== c.mode || shown.harness !== c.harness || shown.where !== c.where)

  const body: ReactNode = (
    <>
      <dl aria-label="What orch will start" className="grid grid-cols-[88px_1fr] gap-x-3 gap-y-1 rounded-md border border-border bg-bg p-3 text-[13px]">
        {facts.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-text-muted">{k}</dt>
            <dd className="text-text">{v}</dd>
          </div>
        ))}
      </dl>
      {warning && (
        <p role="note" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-[13px] text-text">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          {warning}
        </p>
      )}
      <details className="text-[12px] text-text-muted">
        <summary className="cursor-pointer select-none hover:text-text">Details</summary>
        <div className="mt-2 space-y-2">
          {grant && (
            <p>
              Grant <span className="font-mono text-text">{grant.id}</span>
            </p>
          )}
          {/* The exact command core runs, wrapped so all of it is visible. */}
          <pre aria-label="Command" className="whitespace-pre-wrap break-all rounded-md border border-border bg-bg p-3 font-mono text-[12px] leading-5 text-text">
            <code>{c.command}</code>
          </pre>
        </div>
      </details>
      {c.blocked && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-[13px] text-text">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          <div>
            <p>Start is blocked by {c.blocked_by ?? 'an addon'}:</p>
            <p className="mt-0.5 text-text-muted">{cap(c.blocked)}</p>
          </div>
        </div>
      )}
      {differs && (
        <section aria-label={`From addon ${addon}`} className="rounded-md border border-dashed border-addon-border px-3 py-2 text-[12px] text-text-muted">
          <p className="mb-1 flex items-center gap-1.5">
            <AddonBadge name={addon} />
            From addon <span className="font-mono">{addon}</span>: its panel shows something else; orch starts only what is listed above.
          </p>
          <p className="whitespace-pre-wrap break-all font-mono">{[shown.title, shown.mode, shown.harness, shown.where, shown.command].map(cap).filter(Boolean).join(' · ')}</p>
        </section>
      )}
      {c.line && (
        <section aria-label={`From addon ${c.line_by ?? 'launch'}`} className="rounded-md border border-dashed border-addon-border px-3 py-2 text-[12px] text-text-muted">
          <p className="mb-1 flex items-center gap-1.5">
            <AddonBadge name={c.line_by ?? 'launch'} />
            From addon <span className="font-mono">{c.line_by ?? 'launch'}</span>:
          </p>
          <p className="whitespace-pre-wrap break-all">{cap(c.line)}</p>
        </section>
      )}
    </>
  )

  if (!grant && canIssue) {
    return (
      <SignPrompt
        title={`Sign a grant and start ${c.harness} on ${c.ticket}`}
        covers={[`Issues you a grant: all tickets in this workspace, ${GRANT_HOURS} h, until ${hhmm(until)}`, `Starts ${c.harness} on ${c.ticket} under it`]}
        confirmLabel="Sign and start"
        disabled={!!c.blocked}
        onClose={onClose}
        onSign={() => {
          onClose()
          void signed('Grant issued', () => api.issueGrant(ws, { hours: GRANT_HOURS, scope: 'all' })).then((ok) => ok && onStart(launch))
        }}
      >
        {body}
      </SignPrompt>
    )
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg gap-4 border-border bg-surface">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Bot className="size-4 text-brand" aria-hidden />
            {`Start ${c.harness} on ${c.ticket}`}
          </DialogTitle>
          <DialogDescription>orch starts this session under your grant. Only core shows this confirmation; an addon cannot start a session on its own.</DialogDescription>
        </DialogHeader>
        {body}
        {!grant && <p className="text-[13px] text-text-muted">You have no active grant in this workspace. Only owners and maintainers issue grants: ask one to issue yours.</p>}
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!grant || !!c.blocked}
            onClick={() => {
              onClose()
              onStart(launch)
            }}
          >
            Start agent
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
