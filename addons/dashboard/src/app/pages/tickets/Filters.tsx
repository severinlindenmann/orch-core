import { forwardRef, useId } from 'react'
import { Search, X } from 'lucide-react'
import { STATUSES, type Priority, type Status } from '@/api/types'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { FilterSelect } from '../board/Toolbar'
import { PriorityMarker, STATUS_LABEL } from '../board/lib'
import { PRIORITIES, type TicketsSearch } from './search'

export interface FilterOptions {
  types: string[]
  labels: string[]
  people: { value: string; label: string }[]
}

const chip = (on: boolean) =>
  cn(
    'inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-brand',
    on ? 'border-brand bg-brand-soft text-brand' : 'border-input text-text-muted hover:bg-accent hover:text-text',
  )

const toggle = <T,>(list: T[] | undefined, v: T): T[] | undefined => {
  const has = list?.includes(v)
  const next = has ? list!.filter((x) => x !== v) : [...(list ?? []), v]
  return next.length ? next : undefined
}

export const SearchBox = forwardRef<HTMLInputElement, { value: string; onChange: (v: string) => void; placeholder?: string; label?: string }>(function SearchBox(
  { value, onChange, placeholder = 'Search tickets (press /)', label = 'Search tickets' },
  ref,
) {
  return (
    <div className="relative">
      <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-text-faint" aria-hidden />
      <Input
        ref={ref}
        type="search"
        id={useId()}
        name="q"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={label}
        className="h-8 w-[240px] pl-7 text-[12px]"
      />
    </div>
  )
})

export function Filters({
  search,
  onSearch,
  options,
  counts,
  qInput,
  onQInput,
  searchRef,
  dirty,
  onClear,
}: {
  search: TicketsSearch
  onSearch: (patch: Partial<TicketsSearch>) => void
  options: FilterOptions
  counts: Record<Status, number>
  qInput: string
  onQInput: (v: string) => void
  searchRef: React.Ref<HTMLInputElement>
  dirty: boolean
  onClear: () => void
}) {
  return (
    <div className="flex flex-col gap-2" role="toolbar" aria-label="Ticket filters">
      <div className="flex flex-wrap items-center gap-2">
        <SearchBox ref={searchRef} value={qInput} onChange={onQInput} />
        <FilterSelect label="Type" value={search.type ?? 'all'} onChange={(v) => onSearch({ type: v === 'all' ? undefined : v })} options={options.types.map((t) => ({ value: t, label: t }))} />
        <FilterSelect label="People" value={search.person ?? 'all'} onChange={(v) => onSearch({ person: v === 'all' ? undefined : v })} options={options.people} />
        <FilterSelect
          label="Needs"
          value={search.needs ?? 'all'}
          onChange={(v) => onSearch({ needs: v === 'all' ? undefined : (v as TicketsSearch['needs']) })}
          options={[
            { value: 'me', label: 'me' },
            { value: 'agent', label: 'agent' },
            { value: 'nobody', label: 'nobody' },
          ]}
        />
        <FilterSelect label="Label" value={search.label ?? 'all'} onChange={(v) => onSearch({ label: v === 'all' ? undefined : v })} options={options.labels.map((t) => ({ value: t, label: t }))} />
        <span className="mx-1 h-5 w-px bg-border" aria-hidden />
        <div className="flex items-center gap-1" role="group" aria-label="Priority">
          {PRIORITIES.map((p: Priority) => (
            <button key={p} type="button" aria-pressed={!!search.priority?.includes(p)} onClick={() => onSearch({ priority: toggle(search.priority, p) })} className={chip(!!search.priority?.includes(p))}>
              <PriorityMarker priority={p} />
              {p}
            </button>
          ))}
        </div>
        <button
          type="button"
          disabled={!dirty}
          onClick={onClear}
          className="inline-flex h-8 items-center gap-1 rounded-md px-2 text-[12px] text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand disabled:pointer-events-none disabled:opacity-40"
        >
          <X className="size-3.5" aria-hidden /> Clear filters
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Status">
        {STATUSES.map((s) => (
          <button key={s} type="button" aria-pressed={!!search.status?.includes(s)} onClick={() => onSearch({ status: toggle(search.status, s) })} className={chip(!!search.status?.includes(s))}>
            {STATUS_LABEL[s]}
            <span className="font-mono text-[11px] opacity-80">{counts[s]}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
