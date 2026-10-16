// The terminal dock on a real host (M7a): the person's own runs from `run.list` + `run.sub`, a New terminal that shows
// the host's preview before anything starts, Stop, and one RealTerminal for the selected run. Same frame as the
// mockup's dock (header, tabs, resize edge). Start agent (M7b) opens core's Start agent dialog when the host serves it
// (feature `agent_start`); the agent's run then appears here like a shell and attaches the same way.

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { MoreHorizontal, PanelBottomClose, PanelRightClose, Plus, SquareTerminal, Square } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { hasFeature } from '@/api/features'
import { cleanupDoubt, newIdem, outputGone, runQueries, runsApi, type Run, type TermPreview } from '@/api/runs'
import { ApiError } from '@/api/types'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { addonHairline, addonRule } from '@/addon-ui/addonClasses'
import { Inline, Prose, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
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
import { StartAgentDialog } from './StartAgentDialog'

export { EXECUTION_LINE }

/** A run's name in the dock: its label (untrusted, shown through visible.tsx), else the harness and the ticket. */
export function runName(run: Run): string {
  if (run.label) return run.label
  const what = run.harness === 'shell' ? 'Shell' : run.harness === 'claude-code' ? 'Claude' : run.harness === 'codex' ? 'Codex' : 'Terminal'
  return run.ticket ? `${run.ticket} ${what}` : what
}

export function RealDock({ side, size, view, area, setPrefs, collapse, rightFits, autoPreview }: TerminalDockProps & { /** Dev-only layout preview: open New terminal's preview at once. */ autoPreview?: boolean }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const ticket = useDockTicket()
  const qc = useQueryClient()
  const right = side === 'right'
  const list = useQuery({ ...runQueries.list(ws ?? ''), enabled: !!ws })
  const runs = list.data?.runs ?? []
  const shown = runs.filter((r) => r.kind === 'shell' || r.kind === 'agent')
  const [selected, setSelected] = useState<string | null>(null)
  // The run this mount opened itself: its first attach asks for the lease (write, never take).
  const [opened, setOpened] = useState<string | null>(null)
  const [doubt, setDoubt] = useState<Record<string, boolean>>({})
  const [preview, setPreview] = useState<{ args: { ticket?: string }; data: TermPreview | null; error: string; idem: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [agent, setAgent] = useState(false)
  const agents = !!ws && hasFeature('agent_start', ws)
  const [error, setError] = useState('')
  const current = shown.find((r) => r.run === selected) ?? shown.find((r) => r.state === 'running') ?? shown[0]
  const region = useRef<HTMLElement>(null)

  useEffect(() => { setSelected(null) }, [ws])
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
        {!right && <RunTabs runs={shown} current={current?.run ?? null} onSelect={setSelected} doubt={doubt} />}
        {right && <span className="flex-1" />}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size={right ? 'icon-xs' : 'xs'} aria-label="New terminal" disabled={!ws}><Plus />{!right && 'New terminal'}</Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => void ask(false)}>Shell in the workspace</DropdownMenuItem>
            {ticket && <DropdownMenuItem onSelect={() => void ask(true)}>Shell for {ticket}</DropdownMenuItem>}
            <DropdownMenuSeparator />
            {/* agent.preview needs a ticket (contract §12.3): without one the host refuses "args is not valid" */}
            <DropdownMenuItem disabled={!agents || !ticket} onSelect={() => setAgent(true)}>{ticket ? `Start agent on ${ticket}` : 'Start agent'}{!agents ? ' — not available on this host' : !ticket && ' — open a ticket first'}</DropdownMenuItem>
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
          <RunTabs runs={shown} current={current?.run ?? null} onSelect={setSelected} doubt={doubt} />
        </div>
      )}
      {error && <p role="alert" className="shrink-0 border-b border-danger/40 bg-danger-soft px-3 py-1 text-xs text-danger">{error}</p>}
      {off && <p role="status" className="shrink-0 border-b border-border bg-surface px-3 py-1 text-xs text-text-muted">Terminals are switched off for this workspace on this machine. Turn them on in your terminal with <code className="font-mono">orch runner enable</code>.</p>}
      {current && current.grant_state === 'ended' && current.state === 'running' && (
        <p role="status" className="shrink-0 border-b border-border bg-surface px-3 py-1 text-xs text-warning" data-grant-ended>This agent's runner grant ended: the host refuses its writes. Stop it when you are done reading.</p>
      )}
      {current && (doubt[current.run] || (current.exit && cleanupDoubt(current.exit.cleanup))) && (
        <p role="status" className="shrink-0 border-b border-border bg-surface px-3 py-1 text-xs text-warning" data-cleanup-doubt>Some processes may still be running.</p>
      )}
      <div className="min-h-0 min-w-0 flex-1">
        {list.isPending && ws ? <Skeleton className="m-3 h-24" />
          : !current ? <p className="p-3 text-xs text-text-muted">No terminal is open. Start one with New terminal.</p>
          : outputGone(current) ? (
            <div className="p-3 text-xs text-text-muted" data-output-gone>
              <p className="font-medium text-text">Output no longer kept</p>
              <p>{runName(current) === current.label ? <Inline>{current.label}</Inline> : runName(current)} ended{current.exit?.code != null ? ` with code ${current.exit.code}` : ''}. Its output is no longer kept.</p>
            </div>
          ) : ws ? <RealTerminal key={current.run} ws={ws} run={current} initial={opened === current.run ? 'write' : 'read'} /> : null}
      </div>
      {ws && agents && ticket && <StartAgentDialog ws={ws} ticket={ticket} open={agent} onClose={() => setAgent(false)} onStarted={(run) => setSelected(run.run)} />}
      <PreviewDialog preview={preview} busy={busy} onClose={() => setPreview(null)} onConfirm={() => void confirm()} />
    </section>
  )
}

function RunTabs({ runs, current, onSelect, doubt }: { runs: Run[]; current: string | null; onSelect: (id: string) => void; doubt: Record<string, boolean> }) {
  if (runs.length === 0) return <span className="min-w-0 flex-1 truncate text-xs text-text-faint">No terminal open</span>
  return (
    <div role="tablist" aria-label="Terminals" className="relative flex min-w-0 flex-1 items-center gap-1 overflow-x-auto" data-scroll-x>
      {runs.map((r, i) => (
        <button key={r.run} type="button" role="tab" aria-selected={r.run === current} onClick={() => onSelect(r.run)}
          className={cn('flex h-6 max-w-[180px] shrink-0 items-center gap-1 rounded px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-brand', r.run === current ? 'bg-surface-3 text-text' : 'text-text-muted hover:bg-surface-3')}>
          <span aria-hidden="true" className={cn('size-1.5 shrink-0 rounded-full', r.state === 'running' ? 'bg-success' : 'bg-text-faint')} />
          <span className="tabular-nums text-text-faint">{i + 1}</span>
          <span className="min-w-0 truncate">{r.label ? <Inline>{r.label}</Inline> : runName(r)}</span>
          <span className="sr-only">{r.state === 'running' ? 'Running' : 'Ended'}{doubt[r.run] ? ', some processes may still be running' : ''}</span>
        </button>
      ))}
    </div>
  )
}

/** The host's preview, shown before anything starts: what runs, where, as whom. Every value is untrusted text. */
function PreviewDialog({ preview, busy, onClose, onConfirm }: { preview: { data: TermPreview | null; error: string } | null; busy: boolean; onClose: () => void; onConfirm: () => void }) {
  const data = preview?.data
  return (
    <Dialog open={!!preview} onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-w-lg gap-3 border-border bg-surface" data-term-preview>
        <DialogHeader>
          <DialogTitle>Open a terminal</DialogTitle>
          <DialogDescription>{EXECUTION_LINE}</DialogDescription>
        </DialogHeader>
        {!data && !preview?.error && <Skeleton className="h-20" />}
        {data && (
          <dl className="grid grid-cols-[110px_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
            <dt className="text-text-muted">Runs</dt>
            <dd>{data.display !== null ? <Raw>{data.display}</Raw> : <span className="text-text-muted">{data.display_hidden ? 'Hidden: the command has invisible characters' : 'Not shown'}</span>}</dd>
            <dt className="text-text-muted">In</dt>
            <dd>{data.cwd_path !== null ? <Raw>{data.cwd_path}</Raw> : <span className="text-text-muted">{data.cwd_path_hidden ? 'Hidden: the path has invisible characters' : 'Not shown'}</span>}</dd>
            <dt className="text-text-muted">As</dt>
            <dd>{data.notice ? <Inline>{data.notice}</Inline> : 'acts as you, not as an agent'}</dd>
            {data.login_shell && (<><dt className="text-text-muted">Shell</dt><dd>The program is found and started through your login shell; your shell profile runs first and can change what runs.</dd></>)}
            {data.env.length > 0 && (<><dt className="text-text-muted">Environment</dt><dd className="font-mono text-[11px]">{data.env.map((n) => <span key={n} className="mr-2 inline-block"><Inline>{n}</Inline></span>)}</dd></>)}
          </dl>
        )}
        {data?.warnings.map((w, i) => <p key={i} role="note" className="text-xs text-warning"><Prose inline>{w}</Prose></p>)}
        {data?.blocked && (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-xs text-danger">
            Cannot open: {data.blocked.message ? <Prose inline>{data.blocked.message}</Prose> : 'the host refused.'}{data.blocked.code && <span className="ml-1 font-mono text-[11px]">({data.blocked.code})</span>}
          </p>
        )}
        {preview?.error && <p role="alert" className="text-xs text-danger"><Inline>{preview.error}</Inline></p>}
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={!data || !!data.blocked || busy} onClick={onConfirm}>Open terminal</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
