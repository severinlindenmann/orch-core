// "Give agents access for [8 hours]": the one signed choice a start can need (shown only when access is missing or short).
// The sentence under it says the real scope: the grant covers every agent the runner starts, on the tickets they are allowed to.
import { useId } from 'react'
import { nowMs, fmtExact } from '@/lib/time'
import { Label } from '@/components/ui/label'

export function AccessSelect({ hours, onHours, lengths, prefix, agentName, disabled }: { hours: number; onHours: (h: number) => void; lengths: readonly (readonly [number, string])[]; prefix?: string; agentName?: string; disabled?: boolean }) {
  const id = useId()
  const until = new Date(nowMs() + hours * 3_600_000).toISOString()
  return (
    <div className="space-y-1.5" data-testid="agent-access">
      <Label htmlFor={id} className="text-[13px] text-text">Give agents access for</Label>
      <select id={id} value={hours} disabled={disabled} onChange={(e) => onHours(Number(e.target.value))} className="block min-h-10 w-full rounded-md border border-border bg-bg px-3 text-[13px] text-text">
        {lengths.map(([h, text]) => <option key={h} value={h}>{text}</option>)}
      </select>
      <p className="text-[12px] text-text-muted" data-testid="agent-access-until">
        Agents may work on tickets they are allowed to{prefix ? ` in ${prefix}` : ''}, until <time dateTime={until}>{fmtExact(until)}</time>.
        {agentName ? ` Every agent the runner starts can use this access, not only ${agentName}.` : ''}
      </p>
    </div>
  )
}
