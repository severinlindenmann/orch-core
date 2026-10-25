// The terminal dock on a real host (M7a): the person's own runs from `run.list` + `run.sub`, a New terminal that shows
// the host's preview before anything starts, Stop, and one RealTerminal for the selected run. Same frame as the
// mockup's dock (header, tabs, resize edge). Start agent (M7b) opens core's Start agent dialog when the host serves it
// (feature `agent_start`); the agent's run then appears here like a shell and attaches the same way.

import { RunnerSwitch } from '@/components/RunnerSwitch'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal, PanelBottomClose, X, PanelRightClose, Plus, SquareTerminal, Square } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { hasFeature } from '@/api/features'
import { cleanupDoubt, exitText, newIdem, outputGone, runQueries, runsApi, type Run, type TermPreview } from '@/api/runs'
import { ApiError } from '@/api/types'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { addonHairline, addonRule } from '@/addon-ui/addonClasses'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuShortcut, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { useWorkspace } from '../../workspace'
import { DOCK_ADDON, DOCK_KEYS, useDockTicket } from '../dock/context'
import { dockMax, DOCK_LIMITS } from '../dock/prefs'
import type { TerminalDockProps } from '../dock/TerminalDock'
import { ResizeHandle } from '../dock/ResizeHandle'
import { RealTerminal } from './RealTerminal'

import { EXECUTION_LINE } from './executionLine'
import { PreviewDialog } from './PreviewDialog'
import { runName } from './runName'
import { StartAgentDialog } from './StartAgentDialog'

export { EXECUTION_LINE }

export { runName }

export function RealDock({ side, size, view, area, setPrefs, collapse, rightFits, autoPreview, request, requestDone }: TerminalDockProps & { /** Dev-only layout preview: open New terminal's preview at once. */ autoPreview?: boolean }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const ticket = useDockTicket()
  const qc = useQueryClient()
  const right = side === 'right'
  const list = useQuery({ ...runQueries.list(ws ?? ''), enabled: !!ws })
  const runs = list.data?.runs ?? []
  // Ended terminals can be cleared from the dock (this browser only; the run itself stays in the host's list).
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set())
  const ended = (r: Run) => r.state !== 'running' && r.state !== 'starting'
  // A start whose process never started (spawn_failed) is no terminal: the dialog already told the person why, so it gets no tab (unless its cleanup is in doubt: then the tab and the warning stay).
  const neverStarted = (r: Run) => r.state === 'exited' && r.exit?.reason === 'spawn_failed' && !cleanupDoubt(r.exit.cleanup)
  // Terminals switched off for this workspace (the add-on): no shell appears here, same as on the Terminals page. Agent runs stay (they belong to Start agents).
  const termsOn = !!ws && hasFeature('terminals_page', ws)
  const shown = runs.filter((r) => (r.kind === 'agent' || (r.kind === 'shell' && termsOn)) && !neverStarted(r) && !(dismissed.has(r.run) && ended(r)))
  const endedShown = shown.filter(ended)
  const dismiss = (ids: string[]) => setDismissed((d) => new Set([...d, ...ids]))
  const [selected, setSelected] = useState<string | null>(null)
  // The run this mount opened itself: its first attach takes the lease (nobody else can hold a run that was just created).
  const [opened, setOpened] = useState<string | null>(null)
  const [doubt, setDoubt] = useState<Record<string, boolean>>({})
  const [preview, setPreview] = useState<{ args: { ticket?: string }; data: TermPreview | null; error: string; idem: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [agent, setAgent] = useState<'start' | 'choose' | null>(null)
  const agents = !!ws && hasFeature('agent_start', ws)
  const [error, setError] = useState('')
  const current = shown.find((r) => r.run === selected) ?? shown.find((r) => r.state === 'running') ?? shown[0]
  const region = useRef<HTMLElement>(null)

  useEffect(() => { setSelected(null) }, [ws])
  // The Terminals page asked for this run (Open): select it, then say the request is done.
  useEffect(() => { if (request) { setSelected(request); requestDone?.() } }, [request, requestDone])
  // The write attach after Open is one-shot (review M1): once that run's terminal has mounted with it, every later mount
  // (switching tabs and back, a remount) starts read-only. RealTerminal reads `initial` only when it mounts.
  useEffect(() => { if (opened && current?.run === opened) setOpened(null) }, [opened, current?.run])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (autoPreview && ws) void ask(false) }, [autoPreview, ws])

  const resize = (px: number) => setPrefs((p) => ({ ...p, [side]: Math.round(Math.min(dockMax(side, view, area), Math.max(DOCK_LIMITS[side].min, px))) }))
  const ask = async (inTicket: boolean) => {
    if (!ws) return
    const args = inTicket && ticket ? { ticket } : {}
    setPreview({ args, data: null, error: '', idem: newIdem() })
    try {
      const data = await runsApi.preview(ws, args)
      setPreview((p) => (p ? { ...p, data } : p))
    } catch (e) {
      setPreview((p) => (p ? { ...p, error: e instanceof ApiError ? e.message : 'The host did not answer.' } : p))
    }
  }
  const confirm = async () => {
    if (!ws || !preview?.data || preview.data.blocked) return
    setBusy(true)
    try {
      const { run } = await runsApi.open(ws, preview.args, preview.data.spec, preview.idem)
      setOpened(run.run)
      setSelected(run.run)
      setPreview(null)
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
    } catch (e) {
      // The same idem is kept: a retry of this confirmation returns the same run, never a second one.
      setPreview((p) => (p ? { ...p, error: e instanceof ApiError ? e.message : 'The host did not answer.' } : p))
    } finally {
      setBusy(false)
    }
  }
  const stop = async (run: Run) => {
    if (!ws) return
    setError('')
    try {
      const res = await runsApi.stop(ws, run.run)
      if (cleanupDoubt(res.cleanup)) setDoubt((d) => ({ ...d, [run.run]: true }))
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'The run could not be stopped.')
    }
  }
  const runner = list.data?.runner
  const off = runner && (!runner.enabled || !runner.terminals)
  return (
    <section ref={region} tabIndex={-1} aria-label="Terminal dock" data-addon={DOCK_ADDON} data-dock-side={side} data-real-dock
      className={cn('@container/dock relative flex min-w-0 shrink-0 flex-col bg-bg outline-none', addonHairline, right ? 'border-l' : 'border-t')}
      style={right ? { width: size } : { height: size }}>
      <ResizeHandle side={side} size={size} max={dockMax(side, view, area)} onResize={resize} />
      <header className={cn('flex h-9 shrink-0 items-center gap-2 border-b bg-surface-2 px-2 text-xs', addonRule)}>
        <AddonBadge name={DOCK_ADDON} title="Terminals" />
        <SquareTerminal aria-hidden="true" className="size-3.5 shrink-0 text-text-muted" />
        <h2 className="shrink-0 text-xs font-semibold">Terminal</h2>
        {!right && <RunTabs runs={shown} current={current?.run ?? null} onSelect={setSelected} doubt={doubt} onDismiss={(id) => dismiss([id])} />}
        {right && <span className="flex-1" />}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size={right ? 'icon-xs' : 'xs'} aria-label="New terminal" disabled={!ws}><Plus />{!right && 'New terminal'}</Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem disabled={!termsOn} onSelect={() => void ask(false)}>Shell in the workspace{!termsOn && ' — Terminals are off'}</DropdownMenuItem>
            {ticket && <DropdownMenuItem disabled={!termsOn} onSelect={() => void ask(true)}>Shell for {ticket}</DropdownMenuItem>}
            <DropdownMenuSeparator />
            {/* agent.preview needs a ticket (contract §12.3): without one the host refuses "args is not valid" */}
            <DropdownMenuItem disabled={!agents || !ticket} onSelect={() => setAgent('start')}>{ticket ? `Start agent on ${ticket}` : 'Start agent'}{!agents ? ' — not available on this host' : !ticket && ' — open a ticket first'}</DropdownMenuItem>
            {agents && ticket && <DropdownMenuItem onSelect={() => setAgent('choose')}>Change agent or model…</DropdownMenuItem>}
          </DropdownMenuContent>
        </DropdownMenu>
        {current && current.state === 'running' && (
          <Button variant="ghost" size={right ? 'icon-xs' : 'xs'} aria-label={`Stop ${runName(current)}`} onClick={() => void stop(current)}><Square />{!right && 'Stop'}</Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-xs" aria-label="Dock menu"><MoreHorizontal /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {right ? (
              <DropdownMenuItem onSelect={() => setPrefs((p) => ({ ...p, side: 'bottom' }))}>Move to the bottom</DropdownMenuItem>
            ) : (
              <DropdownMenuItem disabled={!rightFits} onSelect={() => setPrefs((p) => ({ ...p, side: 'right' }))}>{rightFits ? 'Move to the right' : 'Not enough room — dock at the bottom'}</DropdownMenuItem>
            )}
            <DropdownMenuItem disabled={endedShown.length === 0} onSelect={() => dismiss(endedShown.map((r) => r.run))}>Clear ended terminals</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={collapse}>Collapse<DropdownMenuShortcut>{DOCK_KEYS}</DropdownMenuShortcut></DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <Button variant="ghost" size="icon-xs" aria-label="Collapse the dock" aria-keyshortcuts="Control+Backquote" onClick={collapse}>
          {right ? <PanelRightClose /> : <PanelBottomClose />}
        </Button>
      </header>
      {right && (
        <div className="flex h-8 shrink-0 items-center border-b border-border bg-surface-2 px-2">
          <RunTabs runs={shown} current={current?.run ?? null} onSelect={setSelected} doubt={doubt} onDismiss={(id) => dismiss([id])} />
        </div>
      )}
      {error && <p role="alert" className="shrink-0 border-b border-danger/40 bg-danger-soft px-3 py-1 text-xs text-danger">{error}</p>}
      {!termsOn && ws && <p role="status" data-terminals-off className="shrink-0 border-b border-border bg-surface px-3 py-1 text-xs text-text-muted">Terminals are switched off in this workspace. Switch them on in Settings, under Add-ons.</p>}
      {termsOn && off && ws && <div role="status" className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border bg-surface px-3 py-1 text-xs text-text-muted">Terminals are switched off for this workspace on this machine.<RunnerSwitch ws={ws} on initialTerminals testId="dock-runner" variant="secondary" /></div>}
      {current && current.grant_state === 'ended' && current.state === 'running' && (
        <p role="status" className="shrink-0 border-b border-border bg-surface px-3 py-1 text-xs text-warning" data-grant-ended>This agent's runner grant ended: the host refuses its writes. Stop it when you are done reading.</p>
      )}
      {current && !outputGone(current) && exitText(current) && <p role="status" className="shrink-0 border-b border-border bg-surface px-3 py-1 text-xs text-text-muted" data-testid="run-exit">{exitText(current)}</p>}
      {current && (doubt[current.run] || (current.exit && cleanupDoubt(current.exit.cleanup))) && (
        <p role="status" className="shrink-0 border-b border-border bg-surface px-3 py-1 text-xs text-warning" data-cleanup-doubt>Some processes may still be running.</p>
      )}
      <div className="min-h-0 min-w-0 flex-1">
        {list.isPending && ws ? <Skeleton className="m-3 h-24" />
          : !current ? <p className="p-3 text-xs text-text-muted">{termsOn ? 'No terminal is open. Start one with New terminal.' : 'No terminal is open.'}</p>
          : outputGone(current) ? (
            <div className="p-3 text-xs text-text-muted" data-output-gone>
              <p className="font-medium text-text">Output no longer kept</p>
              <p>{runName(current) === current.label ? <Inline>{current.label}</Inline> : runName(current)} ended{current.exit?.code != null ? ` with code ${current.exit.code}` : ''}. Its output is no longer kept.</p>
            </div>
          ) : ws ? <RealTerminal key={current.run} ws={ws} run={current} initial={opened === current.run ? 'write' : 'read'} /> : null}
      </div>
      {ws && agents && ticket && <StartAgentDialog key={agent ?? 'none'} ws={ws} ticket={ticket} open={!!agent} choose={agent === 'choose'} onClose={() => setAgent(null)} onStarted={(run) => setSelected(run.run)} />}
      <PreviewDialog preview={preview} busy={busy} onClose={() => setPreview(null)} onConfirm={() => void confirm()} />
    </section>
  )
}

function RunTabs({ runs, current, onSelect, doubt, onDismiss }: { runs: Run[]; current: string | null; onSelect: (id: string) => void; doubt: Record<string, boolean>; onDismiss: (id: string) => void }) {
  if (runs.length === 0) return <span className="min-w-0 flex-1 truncate text-xs text-text-faint">No terminal open</span>
  return (
    <div role="tablist" aria-label="Terminals" className="relative flex min-w-0 flex-1 items-center gap-1 overflow-x-auto" data-scroll-x>
      {runs.map((r, i) => (
        <span key={r.run} className="flex shrink-0 items-center">
        <button type="button" role="tab" aria-selected={r.run === current} onClick={() => onSelect(r.run)}
          className={cn('flex h-6 max-w-[180px] shrink-0 items-center gap-1 rounded px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-brand', r.run === current ? 'bg-surface-3 text-text' : 'text-text-muted hover:bg-surface-3')}>
          <span aria-hidden="true" className={cn('size-1.5 shrink-0 rounded-full', r.state === 'running' ? 'bg-success' : 'bg-text-faint')} />
          <span className="tabular-nums text-text-faint">{i + 1}</span>
          <span className="min-w-0 truncate [&>bdi]:whitespace-nowrap" title={r.label || runName(r)}>{r.label ? <Inline>{r.label}</Inline> : runName(r)}</span>
          <span className="sr-only">{r.state === 'running' ? 'Running' : 'Ended'}{doubt[r.run] ? ', some processes may still be running' : ''}</span>
        </button>
        {r.state !== 'running' && r.state !== 'starting' && <button type="button" aria-label={`Clear ${runName(r)}`} title="Clear this ended terminal" onClick={() => onDismiss(r.run)} className="ml-0.5 flex size-5 shrink-0 items-center justify-center rounded text-text-faint hover:bg-surface-3 hover:text-text"><X className="size-3" aria-hidden /></button>}
        </span>
      ))}
    </div>
  )
}
