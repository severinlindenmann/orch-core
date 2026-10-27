import { DatePicker } from './DatePicker'
import { TimePicker } from './TimePicker'
import { ceilStep, fmtTime, parseTime } from './dates'

/** "YYYY-MM-DDTHH:mm" (local), the same value a datetime-local input held. `min` limits the day and, on min's day,
 * the time too: an earlier time becomes min's time rounded up to the next 15 minutes. */
export function DateTimePicker({ value, onChange, min, ariaLabel = 'Date and time', now }: { value: string; onChange: (v: string) => void; min?: string; ariaLabel?: string; now?: Date }) {
  const [d = '', t = ''] = value.split('T')
  const minDay = min?.slice(0, 10) ?? ''
  const minMins = parseTime(min?.slice(11, 16) ?? '')
  const minTime = minMins === null ? undefined : fmtTime(minMins)
  const earliest = minMins === null ? undefined : fmtTime(ceilStep(minMins, 15))
  const day = d || minDay
  const timeFor = (nd: string) => {
    if (!minDay || nd !== minDay || !earliest) return t || '09:00'
    const tm = parseTime(t)
    return tm === null || tm < minMins! ? earliest : t
  }
  return (
    <div className="grid grid-cols-2 gap-2" role="group" aria-label={ariaLabel}>
      <DatePicker value={d} min={minDay || undefined} now={now} ariaLabel="Day" onChange={(nd) => onChange(`${nd}T${timeFor(nd)}`)} />
      <TimePicker value={t} ariaLabel="Time" minTime={day && day === minDay ? minTime : undefined} onChange={(nt) => onChange(`${day}T${nt}`)} />
    </div>
  )
}
