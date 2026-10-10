import { useRef, useState, type KeyboardEvent } from 'react'
import { Check, ChevronsUpDown } from 'lucide-react'
import { roleOf } from '@/api/permissions'
import type { Workspace } from '@/api/types'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { useAttention } from '../attention'
import { useWorkspace } from '../workspace'
import { SHORTCUTS } from './shortcuts'

/** `⌘2` / `Ctrl+2` as aria-keyshortcuts writes it. */
const ariaKeys = (keys: string) => keys.replace('⌘', 'Meta+').replace('Ctrl+', 'Control+')

/**
 * One calm row (owner feedback G1 #2): prefix, name, a check on the current one, and what needs you when that is more
 * than nothing (the sidebar badge's number). The shortcut shows on hover or focus; the role and the relay are in the
 * tooltip (and in the workspace's settings).
 */
function WorkspaceRow({ w, index, current, viewer, onPick }: { w: Workspace; index: number; current: boolean; viewer: string | undefined; onPick: () => void }) {
  const needs = useAttention(w.id).needsYou.total
  const keys = SHORTCUTS.find((s) => s.id === `workspace.${index + 1}`)?.keys
  const relay = w.relay === 'on' ? 'Relay on (simulated)' : 'Relay not connected'
  return (
    <li>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            data-ws-row
            aria-current={current ? 'true' : undefined}
            onClick={onPick}
            aria-label={`${w.prefix} · ${w.name}${needs > 0 ? `, ${needs} need you` : ''}${current ? ', current workspace' : ''}`}
            aria-keyshortcuts={keys ? ariaKeys(keys) : undefined}
            className="group flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1.5 text-left text-[13px] outline-none hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:ring-2 focus-visible:ring-brand"
          >
            <span className="rounded bg-surface-3 px-1 font-mono text-[10px] font-semibold text-text-muted">{w.prefix}</span>
            <span className="min-w-0 flex-1 truncate">{w.name}</span>
            {keys && (
              <kbd aria-hidden className="hidden rounded bg-surface-3 px-1 font-mono text-[10px] text-text-faint group-hover:inline group-focus-visible:inline">
                {keys}
              </kbd>
            )}
            {needs > 0 && (
              <span aria-label={`${needs} need you`} className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand">
                {needs}
              </span>
            )}
            {current ? <Check role="img" className="size-3.5 shrink-0 text-text-muted" aria-label="Current workspace" /> : <span aria-hidden className="size-3.5 shrink-0" />}
          </button>
        </TooltipTrigger>
        <TooltipContent side="right">
          Your role: {roleOf(w, viewer)} · {relay}
          {keys ? ` · ${keys}` : ''}
        </TooltipContent>
      </Tooltip>
    </li>
  )
}

/** Up/Down (and Home/End) move between the rows; Tab leaves the list as usual. */
function onListKey(e: KeyboardEvent<HTMLUListElement>) {
  const rows = [...e.currentTarget.querySelectorAll<HTMLButtonElement>('[data-ws-row]')]
  const at = rows.indexOf(document.activeElement as HTMLButtonElement)
  const to = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: rows.length - 1 }[e.key]
  if (to === undefined || rows.length === 0) return
  e.preventDefault()
  rows[(to + rows.length) % rows.length].focus()
}

/** The workspace trigger and popover: one calm row per workspace. */
export function WorkspaceSwitcher({ collapsed, viewer }: { collapsed: boolean; viewer: string | undefined }) {
  const { workspace, workspaces, switchWorkspace } = useWorkspace()
  const [open, setOpen] = useState(false)
  const list = useRef<HTMLUListElement>(null)
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
      <PopoverContent
        align="start"
        side="bottom"
        className="w-64 p-1.5"
        onOpenAutoFocus={(e) => {
          // Focus starts on the current workspace (not the first row), so Up/Down move from where you are.
          e.preventDefault()
          ;(list.current?.querySelector<HTMLButtonElement>('[aria-current="true"]') ?? list.current?.querySelector<HTMLButtonElement>('[data-ws-row]'))?.focus()
        }}
      >
        <div id="ws-list-h" className="px-1.5 pb-1 text-[11px] uppercase tracking-wider text-text-faint">
          Workspaces
        </div>
        <ul ref={list} aria-labelledby="ws-list-h" onKeyDown={onListKey} className="space-y-0.5">
          {workspaces.map((w, i) => (
            <WorkspaceRow
              key={w.id}
              w={w}
              viewer={viewer}
              index={i}
              current={w.id === workspace?.id}
              onPick={() => {
                setOpen(false)
                switchWorkspace(w.id)
              }}
            />
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  )
}
