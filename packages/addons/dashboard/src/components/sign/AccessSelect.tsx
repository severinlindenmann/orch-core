// "Give agents access for [8 hours]": the one signed choice a start can need (shown only when access is missing or short).
// The sentence under it says when it ends and that it covers every agent the runner starts; what it allows is the review's own sentence (the signed scope), never restated here.
import { ChevronDown } from 'lucide-react'
import { useId } from 'react'
import { nowMs, fmtExact } from '@/lib/time'
import { Label } from '@/components/ui/label'

export function AccessSelect({ hours, onHours, lengths, prefix, agentName, disabled }: { hours: number; onHours: (h: number) => void; lengths: readonly (readonly [number, string])[]; prefix?: string; agentName?: string; disabled?: boolean }) {
  const id = useId()
  const until = new Date(nowMs() + hours * 3_600_000).toISOString()
  return (
    <div className="space-y-1.5" data-testid="agent-access">
      <p className="text-[13px] text-text">Agents may work for you for {lengths[0]?.[1] ?? `${hours} hours`}.</p>
      <details className="space-y-1.5" data-testid="access-lengths">
        <summary className="flex cursor-pointer items-center gap-1 text-[12px] text-text-muted">Other lengths <ChevronDown className="size-3.5 shrink-0" aria-hidden /></summary>
        <Label htmlFor={id} className="text-[13px] text-text">Give agents access for</Label>
        <select id={id} value={hours} disabled={disabled} onChange={(e) => onHours(Number(e.target.value))} className="block min-h-10 w-full rounded-md border border-border bg-bg px-3 text-[13px] text-text">
          {lengths.map(([h, text]) => <option key={h} value={h}>{text}</option>)}
        </select>
      </details>
      <p className="text-[12px] text-text-muted" data-testid="agent-access-until">
        Access{prefix ? ` in ${prefix}` : ''} ends <time dateTime={until}>{fmtExact(until)}</time>.
        {agentName ? ` Every agent the runner starts can use it, not only ${agentName}.` : ''}
      </p>
    </div>
  )
}
