import { useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import { Check, ChevronsUpDown } from 'lucide-react'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { useWorkspace } from '../workspace'
import { SHORTCUTS } from './shortcuts'

const PREVIEWS = 2

/** Relay is not connected anywhere yet (arrives with orch-relay, P3): a muted dot that says so. */
function RelayDot() {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span role="img" aria-label="Relay not connected" className="size-2 shrink-0 rounded-full border border-text-faint" />
      </TooltipTrigger>
      <TooltipContent side="right">Relay not connected</TooltipContent>
    </Tooltip>
  )
}

function WorkspaceRow({ w, index, current, viewer, onPick }: { w: Workspace; index: number; current: boolean; viewer: string | undefined; onPick: (ticket?: string) => void }) {
  const [today] = useQueries({ queries: [{ queryKey: ['today', w.id], queryFn: () => api.getToday(w.id) }] })
  const previews = (today.data?.needs_you ?? []).slice(0, PREVIEWS)
  const keys = SHORTCUTS.find((s) => s.id === `workspace.${index + 1}`)?.keys
  return (
    <div role="group" aria-label={`${w.prefix} · ${w.name}`} className="rounded-md p-1">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onPick()}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-1.5 py-1 text-left text-[13px] outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-brand"
        >
          <span className="rounded bg-surface-3 px-1 font-mono text-[10px] font-semibold text-text-muted">{w.prefix}</span>
          <span className="min-w-0 flex-1 truncate">{w.name}</span>
          {current && <Check className="size-3.5 text-text-muted" aria-label="Current workspace" />}
        </button>
        {w.needs_you > 0 && (
          <span aria-label={`${w.needs_you} need you`} className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand">
            {w.needs_you}
          </span>
        )}
        <RelayDot />
      </div>
      <div className="flex items-center gap-2 px-1.5 text-[11px] text-text-faint">
        <span>{w.members.find((m) => m.person === viewer)?.role}</span>
        {keys && <kbd className="ml-auto rounded bg-surface-3 px-1 font-mono text-[10px]">{keys}</kbd>}
      </div>
      {previews.length > 0 && (
        <ul className="mt-0.5 space-y-0.5">
          {previews.map((n) => (
            <li key={`${n.kind}:${n.ticket}:${n.ref ?? ''}`}>
              <a
                href={`/ticket/${n.ticket}`}
                onClick={(e) => {
                  e.preventDefault()
                  onPick(n.ticket)
                }}
                className="flex items-baseline gap-2 rounded px-1.5 py-0.5 text-[12px] text-text-muted outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
              >
                <span className="shrink-0 font-mono text-[11px] text-text-faint">{n.ticket}</span>
                <span className="truncate">{n.title}</span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** The workspace trigger and popover: every workspace with your role, what needs you, and the top items to open directly. */
export function WorkspaceSwitcher({ collapsed, viewer }: { collapsed: boolean; viewer: string | undefined }) {
  const { workspace, workspaces, switchWorkspace } = useWorkspace()
  const [open, setOpen] = useState(false)
  const trigger = (
    <button
      type="button"
      aria-label="Switch workspace"
      className={cn(
        'flex w-full items-center gap-2 rounded-md border border-border bg-surface py-1.5 text-left text-[13px] hover:bg-surface-2',
        collapsed ? 'justify-center px-0' : 'px-2',
      )}
    >
      <span className="rounded bg-surface-3 px-1 font-mono text-[10px] font-semibold text-text-muted">{workspace?.prefix ?? '…'}</span>
      <span className={cn('min-w-0 flex-1 truncate', collapsed && 'hidden')}>{workspace?.name}</span>
      <ChevronsUpDown className={cn('size-3.5 text-text-faint', collapsed && 'hidden')} />
    </button>
  )
  return (
    <Popover open={open} onOpenChange={setOpen}>
      {collapsed ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>{trigger}</PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent side="right">{workspace ? `${workspace.prefix} · ${workspace.name}` : 'Workspace'}</TooltipContent>
        </Tooltip>
      ) : (
        <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      )}
      <PopoverContent align="start" side="bottom" className="w-72 p-1.5">
        <div className="px-1.5 pb-1 text-[11px] uppercase tracking-wider text-text-faint">Workspaces</div>
        {workspaces.map((w, i) => (
          <WorkspaceRow
            key={w.id}
            w={w}
            viewer={viewer}
            index={i}
            current={w.id === workspace?.id}
            onPick={(ticket) => {
              setOpen(false)
              switchWorkspace(w.id, { ticket })
            }}
          />
        ))}
      </PopoverContent>
    </Popover>
  )
}
