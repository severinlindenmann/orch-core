// The dock's bottom line in tmux style: [session] 0:shell 1:claude* 2:codex … "DEMO-0043" 11:30 09-Oct. The window
// list is the dock's tabs (one per session window); everything else is simulated decoration.

import { Plus } from 'lucide-react'
import { useRef, type KeyboardEvent } from 'react'
import { harnessOf } from '@/api/harnesses'
import type { TerminalSessionView } from '@/api/terminals'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const clock = (iso: string) => `${iso.slice(11, 16)} ${iso.slice(8, 10)}-${MONTHS[Number(iso.slice(5, 7)) - 1] ?? ''}`

/** A window's tmux name: the harness's window name; a view-only one gets "~", an ended one "!". */
export const windowName = (s: TerminalSessionView, index: number) => `${index}:${harnessOf(s.harness).window}${s.status === 'stopped' ? '!' : s.interactive ? '' : '~'}`

export function TmuxStatus({ name, windows, current, ticket, now, onSelect, onNew, canNew }: {
  name: string
  windows: TerminalSessionView[]
  current: string | null
  ticket?: string
  now?: string
  onSelect: (id: string) => void
  onNew: () => void
  canNew: boolean
}) {
  const list = useRef<HTMLDivElement>(null)
  // Arrow keys move between windows (tabs pattern); Enter/Space select (buttons).
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    const tabs = Array.from(list.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]') ?? [])
    const at = tabs.indexOf(document.activeElement as HTMLButtonElement)
    if (at < 0 || tabs.length === 0) return
    e.preventDefault()
    const next = tabs[(at + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length]
    next.focus()
    next.click()
  }
  return (
    <div className="flex h-6 shrink-0 items-center gap-2 bg-brand-soft px-2 font-mono text-[11px] text-text" data-tmux-status>
      <span className="shrink-0 text-brand" title="tmux session (simulated)">[{name}]</span>
      <div ref={list} role="tablist" aria-label="Session windows" className="flex min-w-0 items-center gap-0.5 overflow-x-auto" onKeyDown={onKey}>
        {windows.map((w, i) => {
          const on = w.id === current
          return (
            <button key={w.id} type="button" role="tab" aria-selected={on} tabIndex={on || (!current && i === 0) ? 0 : -1} title={w.label} onClick={() => onSelect(w.id)}
              className={`shrink-0 rounded-sm px-1 ${on ? 'bg-brand text-on-brand' : 'text-text-muted hover:text-text'}`}>
              {windowName(w, i)}
              {on ? '*' : ''}
              <span className="sr-only"> {w.label}</span>
            </button>
          )
        })}
      </div>
      <button type="button" aria-label="New session" title="New session" disabled={!canNew} onClick={onNew} className="shrink-0 rounded-sm px-1 text-text-muted hover:text-text disabled:opacity-40">
        <Plus className="size-3" />
      </button>
      <span className="flex-1" />
      {ticket && <span className="shrink-0 text-text-muted">"{ticket}"</span>}
      {now && <span className="shrink-0 tabular-nums text-text-muted" title="Mock clock (UTC)">{clock(now)}</span>}
    </div>
  )
}
