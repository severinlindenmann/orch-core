import { Inline } from '@/components/sign/visible'
import { useQuery } from '@tanstack/react-query'
import { Bot, TriangleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { api } from '@/api/client'
import { activeGrantOf, grantTerms, scopeCover } from '@/api/grants'
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
import { addonName } from './SignConfirm'
import { canSpawnAgent } from './capabilities'
import { useAddons } from './slots'
import { fmtClock, fmtExact } from '@/lib/time'
import { queries } from '@/api/queries'

const timeOfDay = (iso: string) => fmtClock(iso)
const withId = (label: string, id: string) => (label === id ? label : `${label} (${id})`)
/** Addon-supplied text, shown in full (never cut): only strings are drawn. */
const text = (v: unknown) => (typeof v === 'string' ? v : '')
/** The addon regions wrap long text and scroll inside a bounded box. */
const REGION = 'max-h-[30vh] overflow-auto rounded-md border border-dashed border-addon-border px-3 py-2 text-[12px] text-text-muted'

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
 * With an active grant it is a plain confirmation; without one, the person signs a grant for themselves here first
 * (SignPrompt, Touch ID) on their own terms (`grantTerms`: members the tickets they may work on, the workspace default).
 */
export function SpawnConfirm({ addon, ticketKey, onStart, onClose }: { addon: string; ticketKey?: string; onStart: (l: ConfirmedLaunch) => void; onClose: () => void }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const signed = useSignedAction()
  const { data: addons } = useAddons()
  const me = useQuery(queries.me())
  const today = useQuery({ ...queries.today(ws!), enabled: !!ws })
  const grants = useQuery({ ...queries.grants(ws!), enabled: !!ws })
  // The same request as the ticket panel (per ticket), or the page's (the ticket picked there).
  const state = useQuery({ ...queries.addonState(ws!, addon, ticketKey), enabled: !!ws })

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
  const doc = useQuery({ ...queries.ticket(request?.ticket as string), enabled: !!request })
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
  // The grant a person signs here when they have none: their own terms (members: the tickets they may work on, the workspace default).
  const terms = grantTerms(role, workspace)
  const canIssue = can(role, 'grant.issue') && !!terms
  const grantHours = terms?.defaultHours ?? 0
  const until = new Date(Date.parse(now) + grantHours * 3600_000).toISOString()
  const facts: [string, string][] = [
    ['Workspace', c.workspace],
    ['Ticket', `${c.ticket} · ${c.title}`],
    // Core's name and, when it differs, the exact id that is posted (launch.mode / harness / where).
    ['Mode', withId(c.mode, launch.mode)],
    ['Harness', withId(c.harness, launch.harness)],
    ['Where', withId(c.where, launch.where)],
    // Rendered by core from the validated plan, never from the addon's text.
    ['Model', `${c.model ? `${c.model}${c.tier ? ` (${c.tier} tier)` : ''}` : 'the harness default'}${c.subagent_model ? `; subagents on ${c.subagent_model}` : ''}`],
    ['Grant', grant ? `active until ${timeOfDay(grant.until)}; revoking it stops this run` : `none yet: signing issues you one for ${terms?.scope === 'workable' ? 'the tickets you may work on' : 'all tickets'} here, ${grantHours} h, until ${timeOfDay(until)}`],
  ]
  const warning = gateWarning(doc.data)
  // What the addon displayed, where it differs from what orch will start.
  // Display name (manifest-written) plus the package id, so no addon passes as another or as core.
  const titleOf = (name: string) => addonName(addons?.find((p) => p.name === name)?.title ?? name, name)
  const differs = shown && (shown.command !== c.command || shown.title !== c.title || shown.mode !== c.mode || shown.harness !== c.harness || shown.where !== c.where)

  const body: ReactNode = (
    <>
      <dl aria-label="What orch will start" className="grid grid-cols-[88px_1fr] gap-x-3 gap-y-1 rounded-md border border-border bg-bg p-3 text-[13px]">
        {facts.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-text-muted">{k}</dt>
            {/* Core's facts; the ticket title in them is the ticket's words, so every fact goes through the visible-string helper. */}
            <dd className="text-text">
              <Inline>{v}</Inline>
            </dd>
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
          <div className="min-w-0 flex-1 space-y-1">
            <p>Start is blocked by the addon {titleOf(c.blocked_by ?? 'launch')}. Its reason:</p>
            {/* The reason is the addon's sentence: labelled as its words, in full. */}
            <section aria-label={`From addon ${c.blocked_by ?? 'launch'}`} className={REGION}>
              <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{text(c.blocked)}</p>
            </section>
          </div>
        </div>
      )}
      {differs && (
        <section aria-label={`From addon ${addon}`} className={REGION}>
          <p className="mb-1 flex items-center gap-1.5">
            <AddonBadge name={addon} />
            From the addon {titleOf(addon)}: its panel shows something else; orch starts only what is listed above.
          </p>
          <p className="whitespace-pre-wrap break-all font-mono">{[shown.title, shown.mode, shown.harness, shown.where, shown.command].map(text).filter(Boolean).join(' · ')}</p>
        </section>
      )}
      {c.line && (
        <section aria-label={`From addon ${c.line_by ?? 'launch'}`} className={REGION}>
          <p className="mb-1 flex items-center gap-1.5">
            <AddonBadge name={c.line_by ?? 'launch'} />
            From the addon {titleOf(c.line_by ?? 'launch')}:
          </p>
          <p className="whitespace-pre-wrap break-all">{text(c.line)}</p>
        </section>
      )}
    </>
  )

  if (!grant && canIssue) {
    return (
      <SignPrompt
        title={`Sign a grant and start ${c.harness} on ${c.ticket}`}
        covers={[`Issues you a grant: ${scopeCover(terms!.scope)}, ${grantHours} h, until ${fmtExact(until)}`, `Starts ${c.harness} on ${c.ticket} under it`]}
        confirmLabel="Sign and start"
        disabled={!!c.blocked}
        onClose={onClose}
        onSign={() => {
          onClose()
          void signed('Grant issued', () => api.issueGrant(ws, { hours: grantHours, scope: terms!.scope })).then((ok) => ok && onStart(launch))
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
        {!grant && <p className="text-[13px] text-text-muted">You have no active grant in this workspace. Viewers cannot start agents: ask an owner to make you a member.</p>}
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
