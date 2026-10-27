// Small local pickers for the "New schedule" form (repeat chips, day-of-month stepper, weekdays). Date, time and
// ticket use the shared pickers in @/components/pickers.
import type { ReactNode } from 'react'

const WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

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

/** A number with a button each way. `wrap` goes round the ends; otherwise it stops at them. */
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
