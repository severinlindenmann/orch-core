import { ACCESS_LENGTHS, accessLength } from '@/api/grants'
import { Button } from '@/components/ui/button'

export function AccessLength({ value, disabled, onChange, variant }: { variant?: 'select'; value: number; disabled: boolean; onChange: (hours: number) => void }) {
  if (variant === 'select') return <div className="space-y-2 text-[13px]">
    <label className="flex flex-wrap items-center gap-2 font-medium">Longest agent access you can give:
      <select aria-label="Longest agent access you can give" className="rounded-md border border-border bg-bg px-3 py-2" value={value} disabled={disabled} onChange={e => onChange(Number(e.target.value))}>
        {!ACCESS_LENGTHS.some(x => x.hours === value) && <option value={value}>{accessLength(value)}</option>}
        {ACCESS_LENGTHS.map(({ hours, label }) => <option key={hours} value={hours}>{label}</option>)}
      </select>
    </label>
    <p className="text-text-muted">You choose when to give access. Existing access keeps its end time.</p>
  </div>
  return <div className="space-y-2">
    <p className="text-[13px] font-medium">Agents may work for you for up to</p>
    <div role="group" aria-label="Agents may work for you for up to" className="flex flex-wrap gap-1 rounded-md border border-border bg-bg p-1">
      {ACCESS_LENGTHS.map(({ hours, label }) => <Button key={hours} size="sm" variant={value === hours ? 'secondary' : 'ghost'} aria-pressed={value === hours} disabled={disabled} onClick={() => onChange(hours)}>{label}</Button>)}
    </div>
    {!ACCESS_LENGTHS.some(x => x.hours === value) && <p className="text-xs text-text-muted">Current limit: {accessLength(value)}</p>}
    <p className="text-xs text-text-muted">The maximum length of new agent access. You still choose when to give access. Existing access keeps its end time.</p>
  </div>
}
