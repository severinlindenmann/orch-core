import { Link } from '@tanstack/react-router'
import { ExternalLink } from 'lucide-react'
import type { ArtifactItem, Member } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { addonHairline, addonTile } from '@/addon-ui/addonClasses'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { KIND_ICON, Thumb } from '../ticket/Artifacts'
import { fmtBytes, fmtTime, Pill } from '../ticket/shared'
import { byLabel } from './label'
import { openMode } from './Preview'

type Open = (a: ArtifactItem, el: HTMLElement) => void

/** A row's identity (a ticket may hold two files of one name with different content). */
export const artifactKey = (a: ArtifactItem) => `${a.ticket}/${a.name}/${a.sha256}`

function NameCell({ a, onOpen }: { a: ArtifactItem; onOpen: Open }) {
  const Icon = KIND_ICON[a.kind]
  const mode = openMode(a)
  const name = <span className="min-w-0 truncate font-medium">{a.name}</span>
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Icon className="size-3.5 shrink-0 text-text-muted" aria-hidden />
      {mode === 'external' ? (
        <a href={a.url} target="_blank" rel="noopener noreferrer nofollow" className="flex min-w-0 items-center gap-1 hover:underline">
          {name}
          <ExternalLink className="size-3 shrink-0 text-text-faint" aria-label="opens in a new tab" />
        </a>
      ) : mode === 'addon' ? (
        <span className="flex min-w-0 items-center gap-1.5">
          {name}
          <AddonBadge name={a.addon!} />
        </span>
      ) : (
        <button type="button" className="min-w-0 truncate text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand" aria-label={`Open ${a.name}`} onClick={(e) => onOpen(a, e.currentTarget)}>
          {name}
        </button>
      )}
    </div>
  )
}

function TicketLink({ a }: { a: ArtifactItem }) {
  return (
    <Link to="/ticket/$key" params={{ key: a.ticket }} className="flex min-w-0 items-baseline gap-1.5 hover:underline" title={a.ticket_title}>
      <span className="shrink-0 font-mono text-[12px]">{a.ticket}</span>
      <span className="truncate text-text-muted">{a.ticket_title}</span>
    </Link>
  )
}

/**
 * The list view. `selected` is the row the preview shows (j/k move it); `narrow`: the preview pane sits beside the
 * table, so "Added by" and "Size" step back (the pane names both).
 */
export function ArtifactList({ items, members, onOpen, selected, narrow = false }: { items: ArtifactItem[]; members: Member[]; onOpen: Open; selected?: string | null; narrow?: boolean }) {
  return (
    <div className="rounded-lg border border-border bg-surface">
      <Table className="table-fixed">
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className={narrow ? 'w-[44%]' : 'w-[32%]'}>Name</TableHead>
            <TableHead className="w-24">Kind</TableHead>
            <TableHead>Ticket</TableHead>
            {!narrow && <TableHead className="w-[18%]">Added by</TableHead>}
            <TableHead className="w-28">Added (UTC)</TableHead>
            {!narrow && <TableHead className="w-20 text-right">Size</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((a) => (
            <TableRow
              key={artifactKey(a)}
              data-kind={a.kind}
              data-artifact={artifactKey(a)}
              aria-current={selected === artifactKey(a) ? 'true' : undefined}
              className={cn('text-[13px]', selected === artifactKey(a) && 'bg-surface-2 shadow-[inset_2px_0_0_var(--color-brand)] hover:bg-surface-2')}
            >
              <TableCell>
                <NameCell a={a} onOpen={onOpen} />
                {a.label && <p className="mt-0.5 truncate pl-5.5 text-[12px] text-text-muted">{a.label}</p>}
              </TableCell>
              <TableCell>
                <Pill>{a.kind}</Pill>
              </TableCell>
              <TableCell>
                <TicketLink a={a} />
              </TableCell>
              {!narrow && <TableCell className="truncate text-text-muted">{byLabel(a.by, members)}</TableCell>}
              <TableCell className="whitespace-nowrap tabular-nums text-text-muted">{fmtTime(a.at)}</TableCell>
              {!narrow && <TableCell className="text-right tabular-nums text-text-muted">{a.bytes > 0 ? fmtBytes(a.bytes) : '–'}</TableCell>}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

export function ArtifactGrid({ items, members, onOpen }: { items: ArtifactItem[]; members: Member[]; onOpen: Open }) {
  return (
    <ul className="grid grid-cols-3 gap-3 xl:grid-cols-4" aria-label="Artifacts">
      {items.map((a) => {
        const Icon = KIND_ICON[a.kind]
        const mode = openMode(a)
        const thumb = a.addon ? (
          <div className={cn('flex h-[120px] w-full items-center justify-center rounded-md border', addonTile)}>
            <AddonBadge name={a.addon} className="size-8 text-lg" />
          </div>
        ) : (
          <Thumb a={{ ...a, added_by: a.by.id }} />
        )
        const body = (
          <>
            {thumb}
            <div className="mt-2 flex items-center gap-1.5">
              <Icon className="size-3.5 shrink-0 text-text-muted" aria-hidden />
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium">{a.name}</span>
              {mode === 'external' && <ExternalLink className="size-3 text-text-faint" aria-hidden />}
            </div>
          </>
        )
        const frame = 'block w-full rounded-md text-left outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50'
        return (
          <li key={artifactKey(a)} data-kind={a.kind} className={cn('rounded-lg border bg-surface p-2.5', a.addon ? addonHairline : 'border-border hover:border-border-strong')}>
            {mode === 'external' ? (
              <a href={a.url} target="_blank" rel="noopener noreferrer nofollow" className={frame}>
                {body}
              </a>
            ) : mode === 'addon' ? (
              <div>{body}</div>
            ) : (
              <button type="button" className={cn(frame, 'cursor-pointer')} aria-label={`Open ${a.name}`} onClick={(e) => onOpen(a, e.currentTarget)}>
                {body}
              </button>
            )}
            <div className="mt-1.5 flex items-center gap-1.5 text-[11px]">
              <Pill>{a.kind}</Pill>
              <Link to="/ticket/$key" params={{ key: a.ticket }} className="font-mono text-text-muted hover:underline" title={a.ticket_title}>
                {a.ticket}
              </Link>
            </div>
            <p className="mt-1 truncate text-[11px] text-text-faint">
              {byLabel(a.by, members)} · {fmtTime(a.at)}
            </p>
          </li>
        )
      })}
    </ul>
  )
}
