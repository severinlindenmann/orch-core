// The shell notice while a delivery waits out its hold window (factory full run, owner decision 2026-10-10 evening;
// docs/factory-full-run-proposal.md). Core's own words, calm (never orange, never a warning colour), one line that
// truncates, like the mandate banner. "Stop…" opens core's decision prompt for that hold (the one option `stop`); the
// person signs it there. Shown to the people who may answer it (core's decisions list is per caller).
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Timer } from 'lucide-react'
import { useEffect, useState } from 'react'
import { addonName } from '@/addon-ui/SignConfirm'
import { useAddons } from '@/addon-ui/slots'
import { queries } from '@/api/queries'
import type { AddonDecision } from '@/api/types'
import { plain } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { fmtClock, nowMs } from '@/lib/time'
import { useDecide } from '../pages/today/rows'
import { useWorkspace } from '../workspace'

/** Whole minutes left in a hold window (never below 0). */
export const holdMinutesLeft = (until: string, now: number) => Math.max(0, Math.ceil((Date.parse(until) - now) / 60_000))

/** A decision core may show as a hold: it says so and has exactly the one option `stop`. */
export const isHold = (d: AddonDecision): d is AddonDecision & { hold: NonNullable<AddonDecision['hold']> } => !!d.hold && d.options.length === 1 && d.options[0].key === 'stop'

export function DeliveryHoldBanner() {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const q = useQuery({ ...queries.addonDecisions(ws!), enabled: !!ws })
  const holds = (q.data ?? []).filter(isHold).sort((a, b) => a.hold.until.localeCompare(b.hold.until))
  // The minutes count down without a refetch: re-render on a tick, read the host's clock at render time.
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!holds.length) return
    const t = setInterval(() => setTick((n) => n + 1), 15_000)
    return () => clearInterval(t)
  }, [holds.length])
  if (!holds.length) return null
  return <HoldLine d={holds[0]} more={holds.length - 1} now={nowMs()} />
}

function HoldLine({ d, more, now }: { d: AddonDecision & { hold: NonNullable<AddonDecision['hold']> }; more: number; now: number }) {
  const { data: packages } = useAddons()
  const pkg = packages?.find((p) => p.name === d.addon)
  const page = pkg?.contributions.find((c) => c.slot === 'nav')?.id
  const { choose, busy, prompt } = useDecide(d)
  const mins = holdMinutesLeft(d.hold.until, now)
  const text = `Delivering in ${mins} min · ${plain(d.hold.deliver_means)} · at ${fmtClock(d.hold.until)} · ${addonName(pkg?.title ?? d.addon, d.addon)}${more > 0 ? ` · ${more} more on hold` : ''}`
  return (
    <div role="region" aria-label="Delivery on hold" data-testid="delivery-hold-banner" className="flex h-9 shrink-0 items-center gap-2 border-b border-border bg-surface px-4 text-[13px]">
      <Timer className="size-4 shrink-0 text-text-muted" aria-hidden />
      <span className="min-w-0 flex-1 truncate text-text" title={`${text}. It goes out on its own unless a person stops it.`} aria-live="polite">
        {text}
      </span>
      <Button size="xs" variant="outline" className="shrink-0" disabled={busy} onClick={() => choose(d.options[0])}>
        Stop…
      </Button>
      {page && (
        <Link to="/addon/$name/$page" params={{ name: d.addon, page }} className="shrink-0 rounded px-1 text-[12px] text-text-muted underline-offset-2 outline-none hover:text-text hover:underline focus-visible:ring-2 focus-visible:ring-ring">
          Details
        </Link>
      )}
      {prompt}
    </div>
  )
}
