import { ACCESS_LENGTHS, accessLength } from '@/api/grants'
import { Button } from '@/components/ui/button'

export function AccessLength({ value, disabled, onChange }: { value: number; disabled: boolean; onChange: (hours: number) => void }) {
  return <div className="space-y-2">
    <p className="text-[13px] font-medium">Agents may work for you for up to</p>
    <div role="group" aria-label="Agents may work for you for up to" className="flex flex-wrap gap-1 rounded-md border border-border bg-bg p-1">
      {ACCESS_LENGTHS.map(({ hours, label }) => <Button key={hours} size="sm" variant={value === hours ? 'secondary' : 'ghost'} aria-pressed={value === hours} disabled={disabled} onClick={() => onChange(hours)}>{label}</Button>)}
    </div>
    {!ACCESS_LENGTHS.some(x => x.hours === value) && <p className="text-xs text-text-muted">Current limit: {accessLength(value)}</p>}
    <p className="text-xs text-text-muted">The maximum length of new agent access. You still choose when to give access. Existing access keeps its end time.</p>
  </div>
}
