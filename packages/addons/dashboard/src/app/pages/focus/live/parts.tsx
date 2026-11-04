import type { ReactNode } from 'react'
import { Check, CircleDashed, Hand, Hourglass, MessageSquare, Pencil, RotateCcw, Search, SquareTerminal, Users } from 'lucide-react'
import type { LiveState, MilestoneIcon } from '@/api/live'
import { cn } from '@/lib/utils'

/** The mockup rule: new UI is marked Preview until the owner takes it in. */
export function PreviewTag({ className }: { className?: string }) {
  return <span className={cn('rounded border border-info/40 bg-info-soft px-1 py-px text-[10px] font-semibold uppercase tracking-wide text-info', className)}>Preview</span>
}

const DOT: Record<LiveState, string> = {
  working: 'bg-brand animate-pulse',
  blocked: 'bg-warning',
  done: 'bg-text-faint',
  failed: 'bg-danger',
  queued: 'border-[1.5px] border-text-faint bg-transparent',
}
export function StateDot({ state, className }: { state: LiveState; className?: string }) {
  return <i aria-hidden data-state={state} className={cn('inline-block size-2 shrink-0 rounded-full', DOT[state], className)} />
}

const ICONS: Record<MilestoneIcon, typeof Check> = { ask: MessageSquare, helper: Users, edit: Pencil, search: Search, retry: RotateCcw, run: SquareTerminal, done: Check, wait: Hand, start: CircleDashed }
export function MilestoneGlyph({ icon, className }: { icon: MilestoneIcon; className?: string }) {
  const Icon = icon === 'wait' ? Hourglass : ICONS[icon]
  return <Icon aria-hidden className={cn('size-3.5', className)} />
}

export function Badge({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn('whitespace-nowrap rounded bg-surface-3 px-1.5 py-0.5 font-mono text-[10.5px] text-text-muted', className)}>{children}</span>
}

export const TOOL_TONE = { claude: 'bg-warning-soft text-warning', codex: 'bg-info-soft text-info', local: 'bg-surface-3 text-text-muted' } as const

export function Seg<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: { id: T; label: string }[]; onChange: (id: T) => void }) {
  return <div role="group" aria-label={label} className="inline-flex shrink-0 rounded-lg border border-border bg-bg p-0.5">
    {options.map((o) => <button key={o.id} type="button" aria-pressed={value === o.id} onClick={() => onChange(o.id)}
      className={cn('h-7 rounded-md px-2.5 text-xs font-medium whitespace-nowrap', value === o.id ? 'bg-surface-3 text-text' : 'text-text-muted hover:text-text')}>{o.label}</button>)}
  </div>
}
