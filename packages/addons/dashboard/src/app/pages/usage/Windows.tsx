// Shared by the Usage page: one tile per window with the agent running time and the run count. Nothing here estimates a
// number: missing is said in words (the page says once that tokens and cost are not available).
import { runsText, spanText, WINDOW_LABEL, type UsageWindow } from '@/api/usage'
import { fmtDay } from '@/lib/time'

export function Tiles({ windows }: { windows: { week: UsageWindow; month: UsageWindow } }) {
  return (
    <section aria-labelledby="usage-time-heading" className="space-y-2">
      <h2 id="usage-time-heading" className="text-[15px] font-semibold">
        Agent running time
      </h2>
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        {(['week', 'month'] as const).map((k) => {
          const win = windows[k]
          return (
            <div key={k} data-testid={`usage-${k}`} className="min-w-0 rounded-lg border border-border bg-surface p-3">
              <p className="text-[11px] font-medium uppercase tracking-wide text-text-faint">{WINDOW_LABEL[k]}</p>
              <p className="mt-1 text-lg font-semibold tabular-nums">{spanText(win)}</p>
              <p className="text-[12px] text-text-muted">{runsText(win)}</p>
              {win.running > 0 && <p className="mt-1 text-[12px] text-text-muted">Includes runs still in progress.</p>}
              {win.counted_since && <p className="mt-1 text-[12px] text-text-muted">Counted since {fmtDay(win.counted_since)}, when orch began keeping this record. Any earlier runs are not included.</p>}
            </div>
          )
        })}
      </div>
      <p className="text-[12px] text-text-muted">Agents that ran at the same time are each counted, so the total can be longer than the time that actually passed.</p>
    </section>
  )
}
