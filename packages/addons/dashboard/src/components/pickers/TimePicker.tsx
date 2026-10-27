import { useState } from 'react'
import { Clock, Minus, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { TIME_CHIPS, fmtTime, parseTime } from './dates'

/** Pick a time of day: Morning / Noon / Afternoon / Evening chips plus a 15-minute stepper. */
export function TimePicker({ value, onChange, id, ariaLabel = 'Time', step = 15, className }: {
  value: string
  onChange: (value: string) => void
  id?: string
  ariaLabel?: string
  step?: number
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const mins = parseTime(value)
  const shown = mins === null ? '09:00' : fmtTime(mins)
  const bump = (d: number) => onChange(fmtTime((mins ?? 9 * 60) + d))
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button id={id} type="button" variant="outline" aria-label={`${ariaLabel}: ${mins === null ? 'not set' : shown}`} className={cn('h-8 w-full justify-start gap-2 px-2 text-[13px] font-normal', mins === null && 'text-text-muted', className)}>
          <Clock className="size-3.5" aria-hidden />
          {mins === null ? 'Pick a time' : shown}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 space-y-3 p-3">
        <div className="flex flex-wrap gap-1.5">
          {TIME_CHIPS.map((c) => (
            <Button key={c.label} type="button" size="sm" variant={c.value === value ? 'default' : 'outline'} onClick={() => { onChange(c.value); setOpen(false) }}>{c.label}</Button>
          ))}
        </div>
        <div className="flex items-center justify-center gap-3">
          <Button type="button" size="icon" variant="outline" className="size-8" aria-label={`${step} minutes earlier`} onClick={() => bump(-step)}><Minus className="size-4" /></Button>
          <output className="w-16 text-center text-lg font-medium tabular-nums" aria-live="polite">{shown}</output>
          <Button type="button" size="icon" variant="outline" className="size-8" aria-label={`${step} minutes later`} onClick={() => bump(step)}><Plus className="size-4" /></Button>
        </div>
        <Button type="button" size="sm" className="w-full" onClick={() => { if (mins === null) onChange(shown); setOpen(false) }}>Done</Button>
      </PopoverContent>
    </Popover>
  )
}
