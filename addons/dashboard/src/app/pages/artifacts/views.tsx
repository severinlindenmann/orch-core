import { Link } from '@tanstack/react-router'
import type { ArtifactItem, Member } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { addonHairline } from '@/addon-ui/addonClasses'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { ArtifactAction, KIND_ICON, LINK, TypeTile, type AddonPages } from '../ticket/Artifacts'
import { fmtBytes, fmtTime, Pill } from '../ticket/shared'
import { byLabel } from './label'
import { artifactKey } from './selection'

export { artifactKey }

interface ViewProps {
  items: ArtifactItem[]
  members: Member[]
  /** The item being previewed (its key). */
  current: string | null
  onPreview: (a: ArtifactItem) => void
  addonPage: AddonPages
}

/**
 * The ticket key, a link that looks like one; the title is plain text beside it (full text on hover). `stacked`: the
 * title on its own line under the key (a narrow table column).
 */
export function TicketLink({ a, className, stacked = false }: { a: Pick<ArtifactItem, 'ticket' | 'ticket_title'>; className?: string; stacked?: boolean }) {
  return (
    <span className={cn('flex min-w-0', stacked ? 'flex-col' : 'items-baseline gap-1.5', className)}>
      <Link to="/ticket/$key" params={{ key: a.ticket }} className={cn(LINK, 'w-fit shrink-0 font-mono text-[12px]')}>
        {a.ticket}
      </Link>
      <span className={cn('min-w-0 truncate text-text-muted', stacked && 'text-[12px]')} title={a.ticket_title}>
        {a.ticket_title}
      </span>
    </span>
  )
}

/**
 * The list view: one row per artifact, plain text except the ticket link and the row's one action at its end
 * (Preview, Open link, or the addon). `compact`: beside the preview pane or on a narrow page, "Kind", "Added by" and
 * "Size" step back (the type icon stays; the viewer names all three).
 */
export function ArtifactList({ items, members, current, onPreview, addonPage, compact = false }: ViewProps & { compact?: boolean }) {
  return (
    <div className="rounded-lg border border-border bg-surface">
      <Table className="table-fixed">
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className={compact ? 'w-[38%]' : 'w-[30%]'}>Name</TableHead>
            {!compact && <TableHead className="w-24">Kind</TableHead>}
            <TableHead>Ticket</TableHead>
            {!compact && <TableHead className="w-[15%]">Added by</TableHead>}
            <TableHead className="w-24">Added (UTC)</TableHead>
            {!compact && <TableHead className="w-18 text-right">Size</TableHead>}
            <TableHead className="w-30">
              <span className="sr-only">Action</span>
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((a) => {
            const Icon = KIND_ICON[a.kind]
            const on = current === artifactKey(a)
            return (
              <TableRow
                key={artifactKey(a)}
                data-kind={a.kind}
                data-artifact={artifactKey(a)}
                aria-current={on ? 'true' : undefined}
                // No hover fill: the row is not a target, its Preview button is.
                className={cn('text-[13px] hover:bg-transparent', on && 'bg-surface-2 shadow-[inset_2px_0_0_var(--color-brand)] hover:bg-surface-2')}
              >
                <TableCell>
                  <div className="flex min-w-0 items-center gap-2">
                    <Icon className="size-3.5 shrink-0 text-text-muted" role="img" aria-label={a.kind} aria-hidden={!compact || undefined}>
                      <title>{a.kind}</title>
                    </Icon>
                    <span className="min-w-0 truncate font-medium" title={a.name}>
                      {a.name}
                    </span>
                    {a.addon && <AddonBadge name={a.addon} />}
                  </div>
                  {a.label && (
                    <p className="mt-0.5 truncate pl-5.5 text-[12px] text-text-muted" title={a.label}>
                      {a.label}
                    </p>
                  )}
                </TableCell>
                {!compact && (
                  <TableCell>
                    <Pill>{a.kind}</Pill>
                  </TableCell>
                )}
                <TableCell>
                  <TicketLink a={a} stacked />
                </TableCell>
                {!compact && <TableCell className="truncate text-text-muted">{byLabel(a.by, members)}</TableCell>}
                <TableCell className="whitespace-nowrap tabular-nums text-text-muted">{fmtTime(a.at)}</TableCell>
                {!compact && <TableCell className="text-right tabular-nums text-text-muted">{a.bytes > 0 ? fmtBytes(a.bytes) : '–'}</TableCell>}
                <TableCell className="whitespace-normal">
                  <ArtifactAction a={a} current={on} addonPage={addonPage} onPreview={() => onPreview(a)} />
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * The grid: the same items, facts and action as the list, as cards of one height. Columns follow the width the
 * results have (a container query), so beside the dock or the preview pane there are fewer.
 */
export function ArtifactGrid({ items, members, current, onPreview, addonPage }: ViewProps) {
  return (
    <ul className="grid grid-cols-1 gap-3 @[28rem]/results:grid-cols-2 @[44rem]/results:grid-cols-3 @[64rem]/results:grid-cols-4" aria-label="Artifacts">
      {items.map((a) => {
        const on = current === artifactKey(a)
        return (
          <li
            key={artifactKey(a)}
            data-kind={a.kind}
            data-artifact={artifactKey(a)}
            aria-current={on ? 'true' : undefined}
            className={cn('flex min-w-0 flex-col rounded-lg border bg-surface p-2.5', a.addon ? addonHairline : 'border-border', on && 'border-brand shadow-[0_0_0_1px_var(--color-brand)]')}
          >
            <TypeTile a={a} />
            <p className="mt-2 truncate text-[13px] font-medium" title={a.name}>
              {a.name}
            </p>
            {/* One line even without a label, so every card has the same height. */}
            <p className="truncate text-[12px] text-text-muted" title={a.label}>
              {a.label ?? ' '}
            </p>
            <TicketLink a={a} className="mt-1.5 text-[12px]" />
            <p className="mt-0.5 truncate text-[11px] text-text-faint">
              {byLabel(a.by, members)} · {fmtTime(a.at)}
              {a.bytes > 0 && ` · ${fmtBytes(a.bytes)}`}
            </p>
            <div className="mt-auto pt-2.5">
              <ArtifactAction a={a} current={on} addonPage={addonPage} onPreview={() => onPreview(a)} />
            </div>
          </li>
        )
      })}
    </ul>
  )
}
