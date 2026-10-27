import { DatePicker } from './DatePicker'
import { TimePicker } from './TimePicker'

/** "YYYY-MM-DDTHH:mm" (local), the same value a datetime-local input held. */
export function DateTimePicker({ value, onChange, min, ariaLabel = 'Date and time', now }: { value: string; onChange: (v: string) => void; min?: string; ariaLabel?: string; now?: Date }) {
  const [d = '', t = ''] = value.split('T')
  return (
    <div className="grid grid-cols-2 gap-2" role="group" aria-label={ariaLabel}>
      <DatePicker value={d} min={min?.slice(0, 10)} now={now} ariaLabel="Day" onChange={(nd) => onChange(`${nd}T${t || '09:00'}`)} />
      <TimePicker value={t} ariaLabel="Time" onChange={(nt) => onChange(`${d || (min ?? '').slice(0, 10)}T${nt}`)} />
    </div>
  )
}
