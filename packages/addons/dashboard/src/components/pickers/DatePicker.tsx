import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { addDays, dateChips, dateLabel, parseDateStr, toDateStr } from './dates'

const WEEKDAYS = [['M', 'Monday'], ['T', 'Tuesday'], ['W', 'Wednesday'], ['T', 'Thursday'], ['F', 'Friday'], ['S', 'Saturday'], ['S', 'Sunday']]
const monthOf = (d: Date) => new Date(d.getFullYear(), d.getMonth(), 1)
/** The same day `n` months on, clamped to the end of the shorter month. */
function addMonths(d: Date, n: number) {
  const last = new Date(d.getFullYear(), d.getMonth() + n + 1, 0).getDate()
  return new Date(d.getFullYear(), d.getMonth() + n, Math.min(d.getDate(), last))
}

/** Pick a day: chips for the usual answers (Today, Tomorrow, Next Monday), a small month for the rest. The month is a
 * real grid: arrow keys move a day or a week, Home/End go to the row ends, PageUp/PageDown change the month. */
export function DatePicker({ value, onChange, min, id, ariaLabel = 'Date', placeholder = 'No date', allowClear, className, now }: {
  value: string
  onChange: (value: string) => void
  min?: string
  id?: string
  ariaLabel?: string
  placeholder?: string
  allowClear?: boolean
  className?: string
  now?: Date
}) {
  const today = now ?? new Date()
  const [open, setOpen] = useState(false)
  const selected = parseDateStr(value)
  const ok = (s: string) => !min || s >= min
  const start = () => value && selected ? value : ok(toDateStr(today)) ? toDateStr(today) : min!
  const [month, setMonth] = useState(() => monthOf(selected ?? today))
  const [focus, setFocus] = useState(start)
  const moved = useRef(false)
  const grid = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!moved.current) return
    moved.current = false
    grid.current?.querySelector<HTMLButtonElement>(`[data-day="${focus}"]`)?.focus()
  }, [focus, month])
  const onOpenChange = (o: boolean) => {
    if (o) { const f = start(); setMonth(monthOf(selected ?? parseDateStr(f) ?? today)); setFocus(f) }
    setOpen(o)
  }
  const first = (month.getDay() + 6) % 7 // Monday first
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  const cells: (Date | null)[] = [...Array(first).fill(null), ...Array.from({ length: days }, (_, i) => new Date(month.getFullYear(), month.getMonth(), i + 1))]
  while (cells.length % 7) cells.push(null)
  const weeks = Array.from({ length: cells.length / 7 }, (_, w) => cells.slice(w * 7, w * 7 + 7))
  const inMonth = (s: string) => s.slice(0, 7) === toDateStr(month).slice(0, 7)
  // Roving tabindex: the focused day if it is shown, else the first day that may be picked.
  const tabbable = inMonth(focus) && ok(focus) ? focus : cells.map((d) => d && toDateStr(d)).find((s) => s && ok(s))
  const pick = (s: string) => { onChange(s); setOpen(false) }
  const onKeyDown = (e: KeyboardEvent) => {
    const at = parseDateStr(tabbable ?? '')
    if (!at) return
    const dow = (at.getDay() + 6) % 7
    const next = ({
      ArrowLeft: () => addDays(at, -1), ArrowRight: () => addDays(at, 1), ArrowUp: () => addDays(at, -7), ArrowDown: () => addDays(at, 7),
      Home: () => addDays(at, -dow), End: () => addDays(at, 6 - dow), PageUp: () => addMonths(at, -1), PageDown: () => addMonths(at, 1),
    } as Record<string, () => Date>)[e.key]
    if (!next) return
    e.preventDefault()
    let to = toDateStr(next())
    if (!ok(to)) to = min! // moving back past min stops at min
    if (to === tabbable) return
    moved.current = true
    setFocus(to)
    setMonth(monthOf(parseDateStr(to)!))
  }
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button id={id} type="button" variant="outline" aria-label={`${ariaLabel}: ${value ? dateLabel(value, today) : placeholder}`} className={cn('h-8 w-full justify-start gap-2 px-2 text-[13px] font-normal', !value && 'text-text-muted', className)}>
          <CalendarDays className="size-3.5" aria-hidden />
          {value ? dateLabel(value, today) : placeholder}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 space-y-3 p-3">
        <div className="flex flex-wrap gap-1.5">
          {dateChips(today).map((c) => (
            <Button key={c.label} type="button" size="sm" variant={c.value === value ? 'default' : 'outline'} disabled={!ok(c.value)} onClick={() => pick(c.value)}>{c.label}</Button>
          ))}
        </div>
        <div className="flex items-center justify-between text-[13px] font-medium">
          <Button type="button" size="icon" variant="ghost" className="size-7" aria-label="Previous month" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}><ChevronLeft className="size-4" /></Button>
          <span aria-live="polite" data-testid="date-picker-month">{month.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })}</span>
          <Button type="button" size="icon" variant="ghost" className="size-7" aria-label="Next month" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}><ChevronRight className="size-4" /></Button>
        </div>
        <div ref={grid} className="space-y-0.5 text-center text-[12px]" role="grid" aria-label="Days" onKeyDown={onKeyDown}>
          <div role="row" className="grid grid-cols-7 gap-0.5">
            {WEEKDAYS.map(([short, long], i) => <span key={i} role="columnheader" aria-label={long} className="py-1 text-text-muted">{short}</span>)}
          </div>
          {weeks.map((week, w) => (
            <div key={w} role="row" className="grid grid-cols-7 gap-0.5">
              {week.map((d, i) => {
                if (!d) return <div key={i} role="gridcell" />
                const s = toDateStr(d)
                return (
                  <div key={i} role="gridcell" className="flex justify-center" aria-selected={s === value}>
                    <button type="button" data-day={s} tabIndex={s === tabbable ? 0 : -1} disabled={!ok(s)} aria-label={d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })} aria-pressed={s === value} onFocus={() => setFocus(s)} onClick={() => pick(s)}
                      className={cn('size-8 rounded-md hover:bg-muted disabled:opacity-35', s === value && 'bg-primary text-primary-foreground hover:bg-primary', s === toDateStr(today) && s !== value && 'font-semibold underline')}>{d.getDate()}</button>
                  </div>
                )
              })}
            </div>
          ))}
        </div>
        {allowClear && value && <Button type="button" size="sm" variant="ghost" onClick={() => pick('')}>No date</Button>}
      </PopoverContent>
    </Popover>
  )
}
