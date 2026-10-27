// swap for shared picker (lane UA): small local pickers for the "New schedule" form (date, time, weekdays).
import type { ReactNode } from 'react'

const WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const TIMES = [
  ['Morning', '09:00'],
  ['Noon', '12:00'],
  ['Afternoon', '15:00'],
  ['Evening', '18:00'],
] as const

/** "2025-11-02" in the browser's own time zone. */
export const isoDay = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
const shift = (d: Date, days: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + days)
/** The next Monday after `d` (a Monday gets the one a week on). */
const nextMonday = (d: Date) => shift(d, ((8 - d.getDay()) % 7) || 7)

export function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`rounded-full border px-2.5 py-1 text-[13px] ${on ? 'border-brand bg-brand/10 text-text' : 'border-border text-text-muted hover:text-text'}`}
    >
      {children}
    </button>
  )
}

interface StepperProps {
  label: string
  value: number
  min: number
  max: number
  step?: number
  wrap?: boolean
  pad?: boolean
  onChange: (value: number) => void
}

/** A number with a button each way. `wrap` goes round the ends (hours, minutes); otherwise it stops at them. */
export function Stepper({ label, value, min, max, step = 1, wrap, pad, onChange }: StepperProps) {
  const move = (dir: 1 | -1) => {
    const v = value + dir * step
    onChange(v > max ? (wrap ? min : max) : v < min ? (wrap ? max : min) : v)
  }
  const btn = 'h-7 w-7 rounded border border-border text-[13px] text-text-muted hover:text-text'
  return (
    <span className="inline-flex items-center gap-1">
      <button type="button" aria-label={`Less ${label}`} className={btn} onClick={() => move(-1)}>
        -
      </button>
      <span aria-label={label} className="w-8 text-center font-mono text-[13px]">
        {String(value).padStart(pad ? 2 : 0, '0')}
      </span>
      <button type="button" aria-label={`More ${label}`} className={btn} onClick={() => move(1)}>
        +
      </button>
    </span>
  )
}

/** Today, Tomorrow, Next Monday and a plain date input. `value` is YYYY-MM-DD. */
export function DateChips({ value, onChange, today = new Date() }: { value: string; onChange: (value: string) => void; today?: Date }) {
  const chips: [string, string][] = [
    ['Today', isoDay(today)],
    ['Tomorrow', isoDay(shift(today, 1))],
    ['Next Monday', isoDay(nextMonday(today))],
  ]
  return (
    <div role="group" aria-label="Day" className="flex flex-wrap items-center gap-2">
      {chips.map(([name, day]) => (
        <Chip key={name} on={value === day} onClick={() => onChange(day)}>
          {name}
        </Chip>
      ))}
      {/* swap for shared picker (lane UA) */}
      <input type="date" aria-label="Date" value={value} onChange={(e) => onChange(e.target.value)} className="rounded border border-border bg-transparent px-2 py-1 text-[13px]" />
    </div>
  )
}

/** Time of day as chips plus an hour and a minute stepper. `value` is HH:MM. */
export function TimeChips({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const hour = Number(value.slice(0, 2)) || 0
  const minute = Number(value.slice(3, 5)) || 0
  const set = (h: number, m: number) => onChange(`${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`)
  return (
    <div role="group" aria-label="Time of day" className="flex flex-wrap items-center gap-2">
      {TIMES.map(([name, time]) => (
        <Chip key={name} on={value === time} onClick={() => onChange(time)}>
          {name} {time}
        </Chip>
      ))}
      <Stepper label="hour" value={hour} min={0} max={23} wrap pad onChange={(h) => set(h, minute)} />
      <span aria-hidden="true">:</span>
      <Stepper label="minute" value={minute} min={0} max={55} step={5} wrap pad onChange={(m) => set(hour, m)} />
    </div>
  )
}

/** Seven weekday chips (Monday = 0). A toggle; the last chosen day stays on. */
export function WeekdayChips({ value, onChange }: { value: number[]; onChange: (value: number[]) => void }) {
  const toggle = (n: number) => {
    if (!value.includes(n)) return onChange([...value, n].sort((a, b) => a - b))
    if (value.length > 1) onChange(value.filter((x) => x !== n))
  }
  return (
    <div role="group" aria-label="Weekdays" className="flex flex-wrap gap-2">
      {WEEK.map((name, n) => (
        <Chip key={name} on={value.includes(n)} onClick={() => toggle(n)}>
          {name}
        </Chip>
      ))}
    </div>
  )
}
