// The dock's session switcher: one tab per window ("1 Claude · Agent"), the state as a small glyph with a tooltip,
// a close button on transcript tabs, and a "+n" menu for the windows that do not fit.

import { Eye, FileText, X } from 'lucide-react'
import { useRef, type KeyboardEvent } from 'react'
import type { TerminalSessionView } from '@/api/terminals'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

export interface DockWindow {
  session: TerminalSessionView
  index: number
  name: string
}

/** A window's state, as a word for the tooltip and the screen reader. */
export const windowState = (s: TerminalSessionView) => (s.status === 'stopped' ? 'Ended' : s.interactive ? 'Running (simulated)' : 'Read only')

function Glyph({ s }: { s: TerminalSessionView }) {
  const word = windowState(s)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span aria-hidden="true" className="inline-flex shrink-0">
          {s.status === 'stopped' ? <FileText className="size-3" /> : s.interactive ? <span className="m-0.5 size-1.5 rounded-full bg-success" /> : <Eye className="size-3" />}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom">{word}</TooltipContent>
    </Tooltip>
  )
}

export function SessionTabs({ windows, current, visible, onSelect, onClose }: {
  windows: DockWindow[]
  current: string | null
  /** How many tabs fit; the rest go into the "+n" menu (the current one always stays visible). */
  visible: number
  onSelect: (id: string) => void
  onClose: (id: string) => void
}) {
  const list = useRef<HTMLDivElement>(null)
  const at = windows.findIndex((w) => w.session.id === current)
  let shown = windows.slice(0, Math.max(1, visible))
  if (at >= shown.length) shown = [...shown.slice(0, -1), windows[at]]
  const hidden = windows.filter((w) => !shown.includes(w))
  // Arrow keys move between tabs (tabs pattern); Enter/Space select (buttons).
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    const tabs = Array.from(list.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])
    const i = tabs.indexOf(document.activeElement as HTMLButtonElement)
    if (i < 0 || tabs.length === 0) return
    e.preventDefault()
    const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]
    next.focus()
    next.click()
  }
  if (windows.length === 0) return <span className="min-w-0 flex-1 truncate text-xs text-text-faint">No session open</span>
  return (
    <div className="flex min-w-0 flex-1 items-center gap-1">
      <div ref={list} role="tablist" aria-label="Session windows" className="flex min-w-0 items-center gap-0.5 overflow-hidden" onKeyDown={onKey}>
        {shown.map((w) => {
          const on = w.session.id === current
          const ended = w.session.status === 'stopped'
          return (
            <span key={w.session.id} className={cn('flex shrink-0 items-center rounded', on ? 'bg-surface-3 text-text' : 'text-text-muted hover:bg-surface-2 hover:text-text')}>
              <button type="button" role="tab" aria-selected={on} tabIndex={on || (!current && w === shown[0]) ? 0 : -1} onClick={() => onSelect(w.session.id)}
                className="flex max-w-56 items-center gap-1.5 truncate px-2 py-0.5 text-xs outline-none focus-visible:ring-2 focus-visible:ring-brand">
                <span className="font-mono text-[10px] text-text-faint">{w.index}</span>
                <span className="truncate">{w.name}</span>
                <Glyph s={w.session} />
                <span className="sr-only">, {windowState(w.session)}</span>
              </button>
              {ended && (
                <button type="button" aria-label={`Close transcript ${w.name}`} onClick={() => onClose(w.session.id)} className="mr-1 rounded p-0.5 text-text-faint hover:text-text">
                  <X className="size-3" />
                </button>
              )}
            </span>
          )
        })}
      </div>
      {hidden.length > 0 && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" aria-label={`${hidden.length} more sessions`} className="shrink-0 rounded px-1.5 py-0.5 text-xs text-text-muted hover:bg-surface-2 hover:text-text">
              +{hidden.length}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            {hidden.map((w) => (
              <DropdownMenuItem key={w.session.id} onSelect={() => onSelect(w.session.id)}>
                <span className="font-mono text-[10px] text-text-faint">{w.index}</span> {w.name} <span className="text-text-faint">· {windowState(w.session)}</span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}
