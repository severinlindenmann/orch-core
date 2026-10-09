import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { CalendarClock, ExternalLink, GitBranch, GitPullRequest, PanelRight } from 'lucide-react'
import type { TicketDocument } from '@/api/types'
import { addonActive } from '@/api/addons'
import { can } from '@/api/permissions'
import { AddonSlotStack, AddonBadge, useAddons, useSlot } from '@/addon-ui'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import { NeedsValue } from './Needs'
import { ago, fmtTime, PersonChip, PriorityLabel, Section, type Viewer } from './shared'

const isHttp = (u: string) => /^https?:\/\//i.test(u)

/** Data an addon wrote on this ticket while it was active; read-only and collapsed until opened. */
function InactiveAddonData({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const { data: addons = [] } = useAddons()
  const [open, setOpen] = useState<string | null>(null)
  const names = workspace ? Object.keys(ticket.addons ?? {}).filter((n) => !addonActive(workspace, n)) : []
  if (names.length === 0) return null
  return (
    <Section title="Inactive addons">
      <ul className="space-y-1.5">
        {names.map((n) => {
          const title = addons.find((a) => a.name === n)?.title ?? n
          const isOpen = open === n
          return (
            <li key={n} className="rounded-md border border-border bg-bg text-text-muted opacity-70">
              <button
                type="button"
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : n)}
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[13px]"
              >
                <AddonBadge name={n} />
                <span>{title} · inactive</span>
              </button>
              {isOpen && (
                <pre className="max-h-60 overflow-auto border-t border-border px-2.5 py-2 font-mono text-[11px] leading-snug text-text-muted">
                  {JSON.stringify(ticket.addons[n], null, 2)}
                </pre>
              )}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

const GITHUB = 'github'

const isPerson = (id: string) => !id.startsWith('agent:')

/** Size, due, blockers, people and times: the ticket's properties (head seq and hash live in Raw). */
function Details({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const row = (label: string, value: React.ReactNode) => (
    <div className="flex items-baseline gap-2 py-1 text-[13px]">
      <dt className="w-24 shrink-0 text-text-muted">{label}</dt>
      <dd className="min-w-0 flex-1">{value}</dd>
    </div>
  )
  const { owner, assignees, reviewers, watchers } = ticket.people
  const people = (all: string[]) => {
    const ids = all.filter(isPerson)
    return ids.length === 0 ? <span className="text-text-faint">none</span> : <span className="flex flex-wrap gap-x-3 gap-y-1">{ids.map((id) => <PersonChip key={id} id={id} viewer={viewer} />)}</span>
  }
  return (
    <Section title="Details">
      <dl className="divide-y divide-border">
        {row('Size', ticket.size ? <span className="uppercase">{ticket.size}</span> : <span className="text-text-faint">not set</span>)}
        {row('Priority', <PriorityLabel priority={ticket.priority} />)}
        {row(
          'Due',
          ticket.due ? (
            <span className="inline-flex items-center gap-1.5">
              <CalendarClock className="size-3.5 text-text-muted" />
              {ticket.due}
            </span>
          ) : (
            <span className="text-text-faint">none</span>
          ),
        )}
        {ticket.blocked_by.length > 0 &&
          row(
            'Blocked by',
            <span className="flex flex-wrap gap-1.5">
              {ticket.blocked_by.map((k) => (
                <Link key={k} to="/ticket/$key" params={{ key: k }} className="font-mono text-[12px] text-brand hover:underline">
                  {k}
                </Link>
              ))}
            </span>,
          )}
        {ticket.needs && row('Needs', <NeedsValue needs={ticket.needs} />)}
        {row('Owner', people(owner ? [owner] : []))}
        {assignees.length > 0 && row('Assignees', people(assignees))}
        {reviewers.length > 0 && row('Reviewers', people(reviewers))}
        {watchers.length > 0 && row('Watchers', people(watchers))}
        {row('Created', <span className="text-text-muted">{fmtTime(ticket.created_at)} UTC</span>)}
        {row('Updated', <span className="text-text-muted">{ago(ticket.updated_at)}</span>)}
      </dl>
    </Section>
  )
}

/** Branches, and pull requests when the GitHub addon does not show them (one place per PR). */
function Code({ ticket, prs }: { ticket: TicketDocument; prs: boolean }) {
  const branches = Object.entries(ticket.links.branches)
  const pulls = prs ? ticket.links.prs : []
  if (branches.length === 0 && pulls.length === 0 && ticket.links.external.length === 0) return null
  return (
    <Section title="Code">
      <ul className="space-y-2 text-[13px]">
        {branches.map(([repo, branch]) => (
          <li key={repo} className="flex items-start gap-2">
            <GitBranch className="mt-0.5 size-3.5 shrink-0 text-text-muted" />
            <span className="min-w-0">
              <span className="block break-all font-mono text-[12px]">{branch}</span>
              <span className="text-[11px] text-text-faint">{repo}</span>
            </span>
          </li>
        ))}
        {pulls.map((pr) => (
          <li key={pr.url} className="flex items-start gap-2">
            <GitPullRequest className="mt-0.5 size-3.5 shrink-0 text-text-muted" />
            {isHttp(pr.url) ? (
              <a href={pr.url} target="_blank" rel="noopener noreferrer nofollow" className="min-w-0 break-all text-brand hover:underline">
                {pr.repo} #{pr.url.split('/').pop()}
                <ExternalLink className="ml-1 inline size-3" aria-hidden />
              </a>
            ) : (
              <span>{pr.repo}</span>
            )}
          </li>
        ))}
        {ticket.links.external.map((l) => (
          <li key={l.url} className="flex items-start gap-2">
            <ExternalLink className="mt-0.5 size-3.5 shrink-0 text-text-muted" />
            {isHttp(l.url) ? (
              <a href={l.url} target="_blank" rel="noopener noreferrer nofollow" className="min-w-0 break-all text-brand hover:underline">
                {l.label}
              </a>
            ) : (
              <span>{l.label}</span>
            )}
          </li>
        ))}
      </ul>
    </Section>
  )
}

/** Everything the rail holds: Details, Code, the addon panels (collapsed, at most two open) and inactive addon data. */
function RailContent({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const { workspace } = useWorkspace()
  return (
    <>
      <Details ticket={ticket} viewer={viewer} />
      <Code ticket={ticket} prs={!addonActive(workspace, GITHUB)} />
      {/* The page made the ticket's workspace current, so viewer.role is the role these actions run under. */}
      <AddonSlotStack name="ticket.panel" ctx={{ ticket }} readOnly={!can(viewer.role, 'addon.action')} collapsible level={3} />
      <InactiveAddonData ticket={ticket} />
    </>
  )
}

/** The rail column next to the content (viewport ≥ 1280 px). */
export function Rail({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  return (
    <aside className="min-w-0 space-y-3" aria-label="Ticket details">
      <RailContent ticket={ticket} viewer={viewer} />
    </aside>
  )
}

/** How many blocks the Panels sheet holds (Details, Code, each addon panel, inactive addon data). */
function usePanelCount(ticket: TicketDocument): number {
  const { workspace } = useWorkspace()
  const addons = useSlot('ticket.panel', { ticket }).length
  const prs = addonActive(workspace, GITHUB) ? 0 : ticket.links.prs.length
  const code = Object.keys(ticket.links.branches).length + prs + ticket.links.external.length > 0 ? 1 : 0
  const inactive = workspace && Object.keys(ticket.addons ?? {}).some((n) => !addonActive(workspace, n)) ? 1 : 0
  return 1 + code + addons + inactive
}

/**
 * Below 1280 px: one line of properties under the header and a "Panels (N)" button that opens the rail as a sheet.
 * The rail never drops under the content.
 */
export function PropertiesStrip({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const [open, setOpen] = useState(false)
  const n = usePanelCount(ticket)
  const branch = Object.values(ticket.links.branches)[0]
  const pr = ticket.links.prs[0]
  const item = (label: string, value: React.ReactNode) => (
    <span className="inline-flex min-w-0 items-baseline gap-1.5">
      <span className="text-text-faint">{label}</span>
      <span className="min-w-0 truncate text-text">{value}</span>
    </span>
  )
  return (
    <div className="flex min-w-0 items-center gap-3 rounded-lg border border-border bg-surface px-3 py-1.5 text-[12px]">
      <p data-testid="ticket-properties" className="flex min-w-0 flex-1 items-center gap-x-4 overflow-hidden whitespace-nowrap">
        {item('Size', ticket.size ? <span className="uppercase">{ticket.size}</span> : 'not set')}
        {item('Due', ticket.due ?? 'none')}
        {ticket.blocked_by.length > 0 && item('Blocked by', <span className="font-mono">{ticket.blocked_by.join(', ')}</span>)}
        {branch && item('Branch', <span className="font-mono">{branch}</span>)}
        {pr && item('PR', `#${pr.url.split('/').pop()}`)}
      </p>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetTrigger asChild>
          <Button size="xs" variant="outline" className="shrink-0">
            <PanelRight />
            Panels ({n})
          </Button>
        </SheetTrigger>
        <SheetContent side="right" className="w-[360px] max-w-[92vw] gap-0 border-border bg-surface p-0 sm:max-w-[360px]">
          <SheetHeader className="border-b border-border">
            <SheetTitle>Panels</SheetTitle>
            <SheetDescription>Details, code and addon panels for {ticket.key}.</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
            <RailContent ticket={ticket} viewer={viewer} />
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
