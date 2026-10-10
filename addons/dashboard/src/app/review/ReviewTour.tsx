import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { ChevronRight, ListChecks } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import { toastApiError } from '../toast'
import { restartToday } from '../todayRestart'
import { useWorkspace } from '../workspace'
import { PERSON_NAME, SCENARIOS, STEP_COUNT, type TourDataset, type TourScenario, type TourStep } from './scenarios'
import { queries } from '@/api/queries'

// Mock only: the review tour for the owner. The "Demo data" pill opens a sheet with the scenarios of REVIEW.md as
// checklists. "Go" sets up a step (person, workspace, demo dataset) and opens its page. Ticks, the open scenarios and
// the last step opened are kept in this browser; storage may be blocked (private window, sandboxed viewer), then they
// last until the page reloads.

export const TICKS_KEY = 'orch.review.ticks'
const OPEN_KEY = 'orch.review.open'
const LAST_KEY = 'orch.review.last'

function readList<T>(key: string, fallback: T[]): T[] {
  try {
    const raw = localStorage.getItem(key)
    if (raw == null) return fallback
    const v: unknown = JSON.parse(raw)
    return Array.isArray(v) ? (v as T[]) : fallback
  } catch {
    return fallback
  }
}

function write(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage unavailable: kept for this page only */
  }
}

const DATASET_NAME: Record<TourDataset, string> = { normal: 'normal', busy: 'busy day' }

type Ask = { kind: 'dataset'; step: TourStep } | { kind: 'reset' }

export function ReviewTour() {
  const qc = useQueryClient()
  const router = useRouter()
  const { workspace, workspaces, setWorkspaceId } = useWorkspace()
  const me = useQuery(queries.me()).data?.person
  const mode: TourDataset = useQuery(queries.devDataset()).data?.dataset ?? 'normal'
  const [open, setOpen] = useState(false)
  const [ticks, setTicks] = useState(() => new Set(readList<string>(TICKS_KEY, [])))
  const [expanded, setExpanded] = useState(() => new Set(readList<number>(OPEN_KEY, [1])))
  const [last, setLast] = useState<string | null>(() => {
    try {
      return localStorage.getItem(LAST_KEY)
    } catch {
      return null
    }
  })
  const [ask, setAsk] = useState<Ask | null>(null)

  const toggleTick = (id: string) => {
    const next = new Set(ticks)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setTicks(next)
    write(TICKS_KEY, [...next])
  }
  const toggleScenario = (n: number) => {
    const next = new Set(expanded)
    if (next.has(n)) next.delete(n)
    else next.add(n)
    setExpanded(next)
    write(OPEN_KEY, [...next])
  }

  // Opens the step's page first; only once the router has really arrived there does it set up the step (dataset,
  // person, workspace). A route blocker (unsaved New ticket overlay, settings drawer, wiki edit) therefore asks
  // before anything changes, and "Keep editing" leaves the demo exactly as it was. The viewer and workspace default
  // to Severin in DEMO, so a step never inherits the person of the step before.
  // The workspace is set with setWorkspaceId, not switchWorkspace: every screen that holds a switch guard also holds
  // a route blocker, so the navigation above has already asked; and switchWorkspace's page-keeping redirects (to
  // Tickets or Today) would undo the page the step just opened.
  const pendingSetup = useRef<(() => void) | null>(null)
  const run = (step: TourStep, switchTo?: TourDataset) => {
    const g = step.go
    if (!g) return
    setAsk(null)
    setOpen(false)
    setLast(step.id)
    try {
      localStorage.setItem(LAST_KEY, step.id)
    } catch {
      /* kept for this page only */
    }
    const setUp = async () => {
      try {
        if (switchTo) {
          await api.resetDemo(switchTo)
          restartToday()
        }
        const viewer = g.viewer ?? 'p_sev'
        if (switchTo || viewer !== me) await api.setViewer(viewer)
        const ws = workspaces.find((w) => w.prefix === (g.workspace ?? 'DEMO'))
        // Replace: the tour already navigated to this page; the step's workspace is the same entry, not a new one.
        if (ws && ws.id !== workspace?.id) setWorkspaceId(ws.id, { url: 'replace' })
        await qc.invalidateQueries()
      } catch (e) {
        toastApiError(e, 'Could not set up that step')
      }
    }
    pendingSetup.current?.()
    pendingSetup.current = null
    if (router.state.location.pathname === g.path) {
      void setUp()
      return
    }
    // One shot: the next resolved navigation either is this step's page (set up) or something else (forget it).
    const unsubscribe = router.subscribe('onResolved', (evt) => {
      unsubscribe()
      pendingSetup.current = null
      if (evt.toLocation.pathname === g.path) void setUp()
    })
    pendingSetup.current = unsubscribe
    router.history.push(g.path)
  }
  const go = (step: TourStep) => {
    if (step.go?.dataset && step.go.dataset !== mode) setAsk({ kind: 'dataset', step })
    else void run(step)
  }
  const reset = async () => {
    setAsk(null)
    try {
      await api.resetDemo()
      setTicks(new Set())
      write(TICKS_KEY, [])
      await qc.invalidateQueries()
      restartToday()
      toast.success('Demo data and ticks reset')
    } catch (e) {
      toastApiError(e, 'Reset failed')
    }
  }

  const done = ticks.size
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 rounded-sm px-1 text-[11px] text-text-muted hover:bg-surface-3 hover:text-text"
      >
        <ListChecks className="size-3" aria-hidden />
        Review tour
        <span className="tabular-nums text-text-faint">
          {done}/{STEP_COUNT}
        </span>
      </button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          side="right"
          className="right-[var(--dock-right,0px)] w-[min(30rem,calc(100vw-var(--dock-right,0px)-4rem))] gap-0 border-border bg-surface sm:max-w-none"
        >
          <SheetHeader className="border-b border-border pr-10">
            <SheetTitle>Review tour</SheetTitle>
            <SheetDescription>
              The scenarios of REVIEW.md. Go opens the page as the right person; tick what you checked. Ticks stay in this
              browser.
            </SheetDescription>
            <p className="text-[12px] text-text-muted" aria-live="polite">
              {done} of {STEP_COUNT} checked
            </p>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto p-3">
            {SCENARIOS.map((s) => (
              <ScenarioSection
                key={s.n}
                scenario={s}
                open={expanded.has(s.n)}
                onToggle={() => toggleScenario(s.n)}
                ticks={ticks}
                onTick={toggleTick}
                onGo={go}
                last={last}
                mode={mode}
              />
            ))}
          </div>
          <div className="border-t border-border p-3">
            <Button variant="outline" size="sm" onClick={() => setAsk({ kind: 'reset' })}>
              Reset demo data and ticks
            </Button>
          </div>
        </SheetContent>
      </Sheet>
      {ask && (
        <Dialog open onOpenChange={(o) => !o && setAsk(null)}>
          <DialogContent role="alertdialog" className="max-w-sm gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>{ask.kind === 'reset' ? 'Reset demo data and ticks?' : `This step uses the ${DATASET_NAME[ask.step.go!.dataset!]} demo`}</DialogTitle>
              <DialogDescription>
                {ask.kind === 'reset'
                  ? `Your demo changes and every tick are discarded. The demo stays on the ${DATASET_NAME[mode]} data.`
                  : 'Switching the demo data discards your demo changes. Your ticks stay.'}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter className="gap-2">
              <Button variant="ghost" onClick={() => setAsk(null)}>
                Cancel
              </Button>
              {ask.kind === 'reset' ? (
                <Button onClick={reset}>Reset</Button>
              ) : (
                <Button onClick={() => void run(ask.step, ask.step.go!.dataset)}>Switch and go</Button>
              )}
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </>
  )
}

function ScenarioSection({
  scenario: s,
  open,
  onToggle,
  ticks,
  onTick,
  onGo,
  last,
  mode,
}: {
  scenario: TourScenario
  open: boolean
  onToggle: () => void
  ticks: Set<string>
  onTick: (id: string) => void
  onGo: (step: TourStep) => void
  last: string | null
  mode: TourDataset
}) {
  const done = s.steps.filter((st) => ticks.has(st.id)).length
  const bodyId = `tour-scenario-${s.n}`
  return (
    <section className="border-b border-border last:border-b-0">
      <h3>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={onToggle}
          className="flex w-full items-center gap-2 rounded-sm px-1 py-2 text-left text-[13px] font-medium text-text hover:bg-surface-2"
        >
          <ChevronRight className={cn('size-3.5 shrink-0 text-text-faint transition-transform', open && 'rotate-90')} aria-hidden />
          <span className="min-w-0 flex-1">
            {s.n}. {s.title}
          </span>
          <span className={cn('shrink-0 text-[11px] tabular-nums', done === s.steps.length ? 'text-success' : 'text-text-faint')}>
            {done}/{s.steps.length}
          </span>
        </button>
      </h3>
      {open && (
        <div id={bodyId} className="pb-3 pl-6">
          <p className="mb-2 text-[12px] text-text-muted">{s.summary}</p>
          <ol className="space-y-2">
            {s.steps.map((st) => {
              const id = `tour-step-${st.id}`
              const g = st.go
              const who = g?.viewer && g.viewer !== 'p_sev' ? `as ${PERSON_NAME[g.viewer]}` : null
              const busy = g?.dataset === 'busy' ? 'Busy day' : null
              return (
                <li key={st.id} className={cn('flex items-start gap-2 rounded-sm py-1 pr-1', last === st.id && 'bg-surface-2')}>
                  <input
                    type="checkbox"
                    id={id}
                    name={id}
                    checked={ticks.has(st.id)}
                    onChange={() => onTick(st.id)}
                    aria-describedby={st.detail ? `${id}-detail` : undefined}
                    className="mt-0.5 size-4 shrink-0 accent-brand"
                  />
                  <div className="min-w-0 flex-1">
                    <label htmlFor={id} className="block text-[13px] text-text">
                      {st.title}
                    </label>
                    {st.detail && (
                      <p id={`${id}-detail`} className="text-[12px] text-text-muted">
                        {st.detail}
                      </p>
                    )}
                    {(who || busy || last === st.id) && (
                      <p className="mt-0.5 flex flex-wrap gap-1 text-[11px] text-text-faint">
                        {who && <span className="rounded-sm border border-border px-1">{who}</span>}
                        {busy && (
                          <span className={cn('rounded-sm border px-1', mode === 'busy' ? 'border-border' : 'border-border-strong text-text-muted')}>{busy}</span>
                        )}
                        {last === st.id && <span>Last opened</span>}
                      </p>
                    )}
                  </div>
                  {g && (
                    <Button size="sm" variant="outline" className="h-7 shrink-0 px-2 text-[12px]" aria-label={`Go: ${s.title} · ${st.title}`} onClick={() => onGo(st)}>
                      Go
                    </Button>
                  )}
                </li>
              )
            })}
          </ol>
        </div>
      )}
    </section>
  )
}
