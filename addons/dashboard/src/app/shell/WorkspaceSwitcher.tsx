import { useId, useRef, useState, type KeyboardEvent, type MutableRefObject } from 'react'
import { Check, ChevronsUpDown, ServerOff } from 'lucide-react'
import { roleOf } from '@/api/permissions'
import type { Workspace } from '@/api/types'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { sharesPrefix, uuid8 } from '@/api/workspaces'
import { visible } from '@/components/sign/visible'
import { cn } from '@/lib/utils'
import { useAttention } from '../attention'
import { useWorkspace } from '../workspace'
import { SHORTCUTS } from './shortcuts'
import { WorkspaceIcon, WorkspaceIconFor, useWorkspaceIcon } from '../workspaceIcon'

/** `⌘2` / `Ctrl+2` as aria-keyshortcuts writes it. */
const ariaKeys = (keys: string) => keys.replace('⌘', 'Meta+').replace('Ctrl+', 'Control+')

/**
 * One calm row (owner feedback G1 #2): prefix, name, a check on the current one, and what needs you when that is more
 * than nothing (the sidebar badge's number). The shortcut shows on hover or focus; the role and the relay are in the
 * tooltip (and in the workspace's settings).
 */
function WorkspaceRow({ w, index, current, viewer, shared, onPick, quietFocus }: { w: Workspace; index: number; current: boolean; viewer: string | undefined; shared: boolean; onPick: () => void; quietFocus: MutableRefObject<boolean> }) {
  const needs = useAttention(w.id).needsYou.total
  const keys = SHORTCUTS.find((s) => s.id === `workspace.${index + 1}`)?.keys
  const relay = w.relay === 'on' ? 'Relay on (simulated)' : 'Relay not connected'
  return (
    <li>
      {/* Hover waits a moment; the focus the popover puts on the current row when it opens shows no tooltip. */}
      <Tooltip delayDuration={500}>
        <TooltipTrigger asChild>
          <button
            type="button"
            data-ws-row
            aria-current={current ? 'true' : undefined}
            onClick={onPick}
            onFocus={(e) => {
              if (!quietFocus.current) return
              quietFocus.current = false
              e.preventDefault() // Radix opens a tooltip on focus unless the event is prevented
            }}
            // `aria-current` says which one is current; the label carries the name and the count once.
            aria-label={`${w.prefix} · ${visible(w.name)}${shared ? ` (${uuid8(w.id)})` : ''}${needs > 0 ? `, ${needs} need you` : ''}`}
            aria-keyshortcuts={keys ? ariaKeys(keys) : undefined}
            className="group flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1.5 text-left text-[13px] outline-none hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:ring-2 focus-visible:ring-brand"
          >
            <WorkspaceIconFor id={w.id} name={w.name} size="sm" />
            <span className="min-w-0 flex-1 truncate">
              <bdi>{visible(w.name)}</bdi>
            </span>
            {shared && (
              <span data-ws-uuid className="font-mono text-[10px] text-text-faint">
                {uuid8(w.id)}
              </span>
            )}
            {keys && (
              <kbd aria-hidden className="hidden rounded bg-surface-3 px-1 font-mono text-[10px] text-text-faint group-hover:inline group-focus-visible:inline">
                {keys}
              </kbd>
            )}
            {needs > 0 && (
              <span aria-hidden className="rounded-full bg-brand px-1.5 text-[11px] font-semibold text-on-brand">
                {needs}
              </span>
            )}
            {current ? <Check aria-hidden className="size-3.5 shrink-0 text-text-muted" /> : <span aria-hidden className="size-3.5 shrink-0" />}
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

/** `PREFIX · name` with the name isolated and its invisible characters shown, then the short id in its own element. */
function WorkspaceLine({ workspace, shared }: { workspace: Workspace; shared: boolean }) {
  return (
    <span>
      {workspace.prefix}
      {shared && (
        <>
          {' '}
          <span className="rounded bg-surface-3 px-1 font-mono" data-ws-id>
            id {uuid8(workspace.id)}
          </span>
        </>
      )}{' '}
      · <bdi>{visible(workspace.name)}</bdi>
    </span>
  )
}

export const NAME_UNSAFE = "This workspace's name can't be shown safely"

/** A workspace whose host does not answer: named by its UUID only (nothing else is known), not selectable. */
function StoppedRow({ w }: { w: Workspace }) {
  const unsafe = w.host_state === 'workspace.name_invalid'
  return (
    <li data-ws-stopped={w.id}>
      <div className="flex w-full min-w-0 items-center gap-2 rounded-md px-1.5 py-1.5 text-[13px] text-text-faint" title={unsafe ? `Workspace ${w.id}: ${NAME_UNSAFE}` : `Workspace ${w.id}: the host does not answer. Start it and reload.`}>
        <ServerOff aria-hidden className="size-3.5 shrink-0" />
        <span className="rounded bg-surface-3 px-1 font-mono text-[10px] font-semibold">{uuid8(w.id)}</span>
        <span className="min-w-0 flex-1 truncate">{unsafe ? NAME_UNSAFE : 'Host not running'}</span>
      </div>
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
  const { workspace, workspaces, unavailable, switchWorkspace } = useWorkspace()
  const [open, setOpen] = useState(false)
  const list = useRef<HTMLUListElement>(null)
  const quietFocus = useRef(false)
  const headingId = useId()
  const mine = useWorkspaceIcon(workspace?.id, workspace?.name)
  const currentShared = !!workspace && workspaces.some((o) => o.id !== workspace.id && sharesPrefix(o, workspace))
  const trigger = (
    <button
      type="button"
      aria-label="Switch workspace"
      className={cn(
        'flex w-full items-center gap-2 rounded-md border border-border bg-surface py-1.5 text-left text-[13px] hover:bg-surface-2',
        collapsed ? 'justify-center px-0' : 'px-1.5',
      )}
    >
      {workspace ? <WorkspaceIcon icon={mine.icon} colour={mine.colour} /> : <span aria-hidden className="size-7 shrink-0 rounded-md bg-surface-3" />}
      <span className={cn('min-w-0 flex-1 truncate', collapsed && 'hidden')}>{workspace ? visible(workspace.name) : null}</span>
      {currentShared && workspace && (
        <span data-ws-uuid-current className={cn('font-mono text-[10px] text-text-faint', collapsed && 'hidden')}>
          {uuid8(workspace.id)}
        </span>
      )}
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
          <TooltipContent side="right">{workspace ? <WorkspaceLine workspace={workspace} shared={currentShared} /> : 'Workspace'}</TooltipContent>
        </Tooltip>
      ) : (
        <Tooltip>
          <TooltipTrigger asChild>
            <PopoverTrigger asChild>{trigger}</PopoverTrigger>
          </TooltipTrigger>
          <TooltipContent side="right">{workspace ? <WorkspaceLine workspace={workspace} shared={currentShared} /> : 'Workspace'}</TooltipContent>
        </Tooltip>
      )}
      <PopoverContent
        align="start"
        side="bottom"
        className="w-64 p-1.5"
        onOpenAutoFocus={(e) => {
          // Focus starts on the current workspace (not the first row), so Up/Down move from where you are.
          e.preventDefault()
          quietFocus.current = true
          ;(list.current?.querySelector<HTMLButtonElement>('[aria-current="true"]') ?? list.current?.querySelector<HTMLButtonElement>('[data-ws-row]'))?.focus()
        }}
      >
        <div id={headingId} className="px-1.5 pb-1 text-[11px] uppercase tracking-wider text-text-faint">
          Workspaces
        </div>
        <ul ref={list} aria-labelledby={headingId} onKeyDown={onListKey} className="space-y-0.5">
          {workspaces.map((w, i) => (
            <WorkspaceRow
              key={w.id}
              w={w}
              viewer={viewer}
              index={i}
              shared={workspaces.some((o) => o.id !== w.id && sharesPrefix(o, w))}
              current={w.id === workspace?.id}
              quietFocus={quietFocus}
              onPick={() => {
                setOpen(false)
                switchWorkspace(w.id)
              }}
            />
          ))}
          {unavailable.map((w) => (
            <StoppedRow key={w.id} w={w} />
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  )
}
