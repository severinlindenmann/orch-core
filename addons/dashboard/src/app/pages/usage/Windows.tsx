// Shared by the Usage page and the ticket box: one tile per window with runs and session time, and the honest line about
// tokens and cost. Nothing here estimates a number: missing is said in words.
import { runsText, spanText, NOT_REPORTED, WINDOW_LABEL, type UsageWindow } from '@/api/usage'
import { fmtDay } from '@/lib/time'

export function Tiles({ windows }: { windows: { week: UsageWindow; month: UsageWindow } }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {(['week', 'month'] as const).map((k) => {
        const win = windows[k]
        return (
          <div key={k} data-testid={`usage-${k}`} className="rounded-lg border border-border bg-surface p-3">
            <p className="text-[11px] font-medium uppercase tracking-wide text-text-faint">{WINDOW_LABEL[k]}</p>
            <p className="mt-1 text-lg font-semibold tabular-nums">{spanText(win)}</p>
            <p className="text-[12px] text-text-muted">{runsText(win)}</p>
            {win.counted_since && <p className="mt-1 text-[12px] text-text-muted">Counted since {fmtDay(win.counted_since)}: runs before that were not recorded here.</p>}
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 text-[12px] text-text-muted">
              <dt>Tokens</dt>
              <dd data-testid={`usage-${k}-tokens`}>{NOT_REPORTED}</dd>
              <dt>Cost</dt>
              <dd data-testid={`usage-${k}-cost`}>{NOT_REPORTED}</dd>
            </dl>
          </div>
        )
      })}
    </div>
  )
}
