import { useState } from 'react'
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { dateChips, dateLabel, parseDateStr, toDateStr } from './dates'

/** Pick a day: chips for the usual answers (Today, Tomorrow, Next Monday), a small month for the rest. */
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
  const [month, setMonth] = useState(() => new Date((selected ?? today).getFullYear(), (selected ?? today).getMonth(), 1))
  const first = (month.getDay() + 6) % 7 // Monday first
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  const cells: (Date | null)[] = [...Array(first).fill(null), ...Array.from({ length: days }, (_, i) => new Date(month.getFullYear(), month.getMonth(), i + 1))]
  const ok = (s: string) => !min || s >= min
  const pick = (s: string) => { onChange(s); setOpen(false) }
  return (
    <Popover open={open} onOpenChange={setOpen}>
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
          <span aria-live="polite">{month.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })}</span>
          <Button type="button" size="icon" variant="ghost" className="size-7" aria-label="Next month" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}><ChevronRight className="size-4" /></Button>
        </div>
        <div className="grid grid-cols-7 gap-0.5 text-center text-[12px]" role="grid" aria-label="Days">
          {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((d, i) => <span key={i} className="py-1 text-text-muted" aria-hidden>{d}</span>)}
          {cells.map((d, i) => d ? (
            <button key={i} type="button" disabled={!ok(toDateStr(d))} aria-label={d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })} aria-pressed={toDateStr(d) === value} onClick={() => pick(toDateStr(d))}
              className={cn('size-8 rounded-md hover:bg-muted disabled:opacity-35', toDateStr(d) === value && 'bg-primary text-primary-foreground hover:bg-primary', toDateStr(d) === toDateStr(today) && toDateStr(d) !== value && 'font-semibold underline')}>{d.getDate()}</button>
          ) : <span key={i} />)}
        </div>
        {allowClear && value && <Button type="button" size="sm" variant="ghost" onClick={() => pick('')}>No date</Button>}
      </PopoverContent>
    </Popover>
  )
}
