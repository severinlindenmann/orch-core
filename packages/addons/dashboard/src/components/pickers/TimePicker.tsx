import { useState } from 'react'
import { Clock, Minus, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { TIME_CHIPS, ceilStep, clampTime, fmtTime, lastStep, parseTime } from './dates'

/** Pick a time of day: Morning / Noon / Afternoon / Evening chips plus a 15-minute stepper. The stepper stays within
 * the day (00:00 to the last step, 23:45) and, with `minTime` ("HH:MM"), never goes before it (rounded up to a step). */
export function TimePicker({ value, onChange, id, ariaLabel = 'Time', step = 15, minTime, className }: {
  value: string
  onChange: (value: string) => void
  id?: string
  ariaLabel?: string
  step?: number
  minTime?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const mins = parseTime(value)
  const floor = minTime ? parseTime(minTime) : null
  const lo = floor === null ? 0 : ceilStep(floor, step)
  const hi = lastStep(step)
  const base = mins ?? (floor === null ? 9 * 60 : lo)
  const shown = fmtTime(base)
  const bump = (d: number) => onChange(fmtTime(clampTime(base + d, lo, hi)))
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
            <Button key={c.label} type="button" size="sm" variant={c.value === value ? 'default' : 'outline'} disabled={floor !== null && parseTime(c.value)! < floor} onClick={() => { onChange(c.value); setOpen(false) }}>{c.label}</Button>
          ))}
        </div>
        <div className="flex items-center justify-center gap-3">
          <Button type="button" size="icon" variant="outline" className="size-8" aria-label={`${step} minutes earlier`} disabled={base <= lo} onClick={() => bump(-step)}><Minus className="size-4" /></Button>
          <output className="w-16 text-center text-lg font-medium tabular-nums" aria-live="polite">{shown}</output>
          <Button type="button" size="icon" variant="outline" className="size-8" aria-label={`${step} minutes later`} disabled={base >= hi} onClick={() => bump(step)}><Plus className="size-4" /></Button>
        </div>
        <Button type="button" size="sm" className="w-full" onClick={() => { if (mins === null) onChange(shown); setOpen(false) }}>Done</Button>
      </PopoverContent>
    </Popover>
  )
}
