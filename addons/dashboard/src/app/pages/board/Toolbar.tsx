import { LayoutList, Search, SquareKanban, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { DisplayPopover } from './DisplayPopover'
import { NO_FILTERS, type BoardDisplay, type Filters } from './lib'
import { AddonBadge } from '@/addon-ui'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

export type View = 'board' | 'list'

export function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: { value: string; label: string }[]
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger
        size="sm"
        aria-label={label}
        className={cn('h-8 min-w-[110px] gap-1.5 text-[12px]', value !== 'all' && 'border-brand text-brand')}
      >
        <span className="text-text-faint">{label}</span>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="all">All</SelectItem>
        {options.map((o) => (
          <SelectItem key={o.value} value={o.value}>
            {o.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

export function Toolbar({
  view,
  onView,
  filters,
  onFilters,
  types,
  labels,
  people,
  epics,
  shown,
  total,
  display,
  onDisplay,
  moveLimit,
  columns,
  onJump,
}: {
  view: View
  onView: (v: View) => void
  filters: Filters
  onFilters: (f: Filters) => void
  types: string[]
  labels: string[]
  people: { value: string; label: string }[]
  epics: { value: string; label: string }[]
  shown: number
  total: number
  display: BoardDisplay
  onDisplay: (patch: Partial<BoardDisplay>) => void
  /** 'viewer': read only; 'cannot-move': can act but not move; null: can move. */
  moveLimit: 'viewer' | 'cannot-move' | null
  /** Column counts for the jump chips (board view only). */
  columns: { key: string; label: string; count?: number; addon?: string }[] | null
  onJump: (key: string) => void
}) {
  const set = (patch: Partial<Filters>) => onFilters({ ...filters, ...patch })
  const dirty = JSON.stringify(filters) !== JSON.stringify(NO_FILTERS)
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Board filters">
        <ToggleGroup type="single" value={view} onValueChange={(v) => v && onView(v as View)} variant="outline" size="sm" aria-label="View">
          <ToggleGroupItem
            value="board"
            aria-label="Board view"
            className="h-8 gap-1.5 px-2.5 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand"
          >
            <SquareKanban className="size-3.5" /> Board
          </ToggleGroupItem>
          <ToggleGroupItem
            value="list"
            aria-label="List view"
            className="h-8 gap-1.5 px-2.5 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand"
          >
            <LayoutList className="size-3.5" /> List
          </ToggleGroupItem>
        </ToggleGroup>

        <span className="mx-1 h-5 w-px bg-border" aria-hidden />

        <button
          type="button"
          aria-pressed={filters.mine}
          onClick={() => set({ mine: !filters.mine })}
          className={cn(
            'h-8 rounded-md border px-2.5 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-brand',
            filters.mine ? 'border-brand bg-brand-soft text-brand' : 'border-input text-text-muted hover:bg-accent hover:text-text',
          )}
        >
          Mine
        </button>
        <FilterSelect label="Type" value={filters.type} onChange={(type) => set({ type })} options={types.map((t) => ({ value: t, label: t }))} />
        <FilterSelect
          label="Label"
          value={filters.label}
          onChange={(label) => set({ label })}
          options={labels.map((t) => ({ value: t, label: t }))}
        />
        <FilterSelect label="Person" value={filters.person} onChange={(person) => set({ person })} options={people} />
        <FilterSelect label="Epic" value={filters.epic} onChange={(epic) => set({ epic })} options={epics} />

        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-text-faint" aria-hidden />
          <Input
            value={filters.q}
            onChange={(e) => set({ q: e.target.value })}
            placeholder="Filter tickets"
            aria-label="Filter tickets"
            className="h-8 w-[200px] pl-7 text-[12px]"
          />
        </div>
        {dirty && (
          <button
            type="button"
            onClick={() => onFilters(NO_FILTERS)}
            className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-[12px] text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
          >
            <X className="size-3.5" /> Clear
          </button>
        )}
        <span className="flex-1" />
        {moveLimit === 'viewer' && (
          <span className="rounded-md border border-border px-2 py-1 text-[11px] text-text-muted">Read only</span>
        )}
        {moveLimit === 'cannot-move' && (
          <Tooltip>
            <TooltipTrigger asChild>
              <span tabIndex={0} className="rounded-md border border-border px-2 py-1 text-[11px] text-text-muted outline-none focus-visible:ring-2 focus-visible:ring-brand">
                Can't move tickets
              </span>
            </TooltipTrigger>
            <TooltipContent>Your role can't move tickets</TooltipContent>
          </Tooltip>
        )}
        {view === 'board' && <DisplayPopover display={display} onChange={onDisplay} />}
        <span className="font-mono text-[11px] text-text-faint" aria-live="polite">
          {shown === total ? `${total} tickets` : `${shown} of ${total}`}
        </span>
      </div>
      {columns && (
        <nav aria-label="Jump to column" className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-text-faint">Jump to</span>
          {columns.map((c) => (
            <button
              key={c.key}
              type="button"
              onClick={() => onJump(c.key)}
              className="inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[11px] text-text-muted outline-none hover:bg-accent hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
            >
              {c.addon && <AddonBadge name={c.addon} />}
              {c.label}
              {c.count !== undefined && <span className="font-mono text-text-faint">{c.count}</span>}
            </button>
          ))}
        </nav>
      )}
    </div>
  )
}
