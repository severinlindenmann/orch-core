// Start agent on a real host (M7b, contract v1.6 sections 12.3 and 12.4). Core's own dialog, never an addon frame: the
// host's preview first (what runs, where, with which runner grant, what blocks it), then an explicit Start that posts
// exactly the previewed values plus the preview's `spec`, with one idem per dialog opening (a retry returns the same
// run, never a second process). `launch.changed` previews again and asks for a new Start. Every host string is
// untrusted text (visible.tsx). The runner grant is the only signed step: a click request whose secret goes from the
// signer to the runner, never through this page.

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AGENT_MODES, agentsApi, blockedText, cwdText, DEPOSIT_UNKNOWN, depositReason, HARNESSES, runnerQuery, SHARED_COPY, TERMINAL_GRANT,
  TERMINAL_ONLY, UPDATE_ORCH, worktreeLine, type AgentArgs, type AgentMode, type AgentPreview, type Harness, type Runner,
} from '@/api/agents'
import { clickReady, DEPOSIT_RISK, depositViewOk, type ClickAction } from '@/api/click'
import { newIdem, type Run } from '@/api/runs'
import { ApiError } from '@/api/types'
import { ClickSignPanel } from '@/components/sign/ClickSignPanel'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { useSignMethods } from '@/components/sign/SignMethods'
import { useClickSign } from '@/components/sign/useClickSign'
import { Inline, Prose, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { queries } from '@/api/queries'
import { grantDefaultHours, runnerLengths } from '@/api/grants'
import { fmtDateTime, nowMs } from '@/lib/time'
import { EXECUTION_LINE } from './executionLine'

/** One plain line per mode, shown under the choice. */
export const MODE_HELP: Record<AgentMode, string> = {
  refine: 'Helps you write the requirements and the plan. It changes no code.',
  work: 'Works through the ticket: writes the code and submits it for review.',
  fix: 'Fixes what a reviewer or a check sent back.',
  continue: 'Picks up where an earlier agent stopped.',
  context: 'Reads the ticket and the code, so you can ask questions. It changes nothing.',
}

const MODEL = /^[A-Za-z0-9][A-Za-z0-9._:[\]/-]{0,63}$/
const DEPOSIT_POLLS = 5
const DEPOSIT_POLL_MS = 2000

const same = (a: AgentArgs, b: AgentArgs) => JSON.stringify(a) === JSON.stringify(b)
const message = (e: unknown) => (e instanceof ApiError ? e.message : 'The dashboard server did not answer.')
const clock = fmtDateTime

/** The form's values as `agent.preview`'s arguments: empty optionals left out; the subagent model is Claude Code's only. */
function argsOf(f: { ticket?: string; harness: Harness; mode: AgentMode; model: string; subagent: string; label: string; worktree: boolean }): AgentArgs {
  return {
    ...(f.ticket ? { ticket: f.ticket } : {}),
    harness: f.harness,
    mode: f.mode,
    ...(f.model ? { model: f.model } : {}),
    ...(f.harness === 'claude-code' && f.subagent ? { subagent_model: f.subagent } : {}),
    ...(f.label ? { label: f.label } : {}),
    worktree: f.worktree,
  }
}

export function StartAgentDialog({ ws, ticket, open, onClose, onStarted, grantStep }: { ws: string; ticket?: string; open: boolean; onClose: () => void; onStarted?: (run: Run) => void; /** Dev layout preview only: the grant card's starting step. */ grantStep?: GrantStep }) {
  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-h-[90vh] max-w-xl gap-3 overflow-y-auto border-border bg-surface" data-start-agent>
        {open && <StartAgent ws={ws} ticket={ticket} onClose={onClose} onStarted={onStarted} grantStep={grantStep} />}
      </DialogContent>
    </Dialog>
  )
}

function StartAgent({ ws, ticket, onClose, onStarted, grantStep }: { ws: string; ticket?: string; onClose: () => void; onStarted?: (run: Run) => void; grantStep?: GrantStep }) {
  const qc = useQueryClient()
  // One idem per dialog opening, kept for every retry of Start (the run id is `r_` + its ULID).
  const [idem] = useState(newIdem)
  const [harness, setHarness] = useState<Harness>('claude-code')
  const [mode, setMode] = useState<AgentMode>('work')
  const [model, setModel] = useState('')
  const [subagent, setSubagent] = useState('')
  const [label, setLabel] = useState('')
  const [worktree, setWorktree] = useState(true)
  const form = argsOf({ ticket, harness, mode, model, subagent, label, worktree })
  const [shown, setShown] = useState<{ args: AgentArgs; data: AgentPreview } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const asked = useRef(0)
  const runner = useQuery({ ...runnerQuery(ws) })

  const preview = useCallback(async (args: AgentArgs) => {
    const n = ++asked.current
    setLoading(true)
    setError('')
    try {
      const data = await agentsApi.preview(ws, args)
      if (n === asked.current) setShown({ args, data })
    } catch (e) {
      if (n === asked.current) { setShown(null); setError(message(e)) }
    } finally {
      if (n === asked.current) setLoading(false)
    }
  }, [ws])
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void preview(form) }, [])

  const badModel = (model && !MODEL.test(model)) || (subagent && harness === 'claude-code' && !MODEL.test(subagent))
  const badLabel = label.length > 40
  const stale = !shown || !same(shown.args, form)
  const data = shown?.data
  const start = async () => {
    if (!shown || stale || !data?.spec || data.blocked || busy) return
    setBusy(true)
    setError('')
    setNote('')
    try {
      const { run } = await agentsApi.start(ws, shown.args, data.spec, idem)
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
      onStarted?.(run)
      onClose()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'launch.changed') {
        // What would run changed since the preview (also a new runner grant): show it again, ask for a new Start.
        setNote('What would run changed since the preview. Check it again, then start.')
        await preview(shown.args)
      } else {
        // The same idem stays: Start again returns the recorded run, never a second one. A lost answer: read the list.
        if (!(e instanceof ApiError) || e.status >= 500) void qc.invalidateQueries({ queryKey: ['runs', ws] })
        setError(message(e))
      }
    } finally {
      setBusy(false)
    }
  }
  const noGrant = data?.blocked?.code === 'launch.no_grant'
  // Review L2: nothing starts that the person could not read (a command or a folder withheld for invisible characters).
  const unreadable = !!data && (data.display_hidden || data.cwd_path_hidden || data.display === null || data.cwd_path === null)

  return (
    <>
      <DialogHeader>
        <DialogTitle>Start agent{ticket ? ` on ${ticket}` : ''}</DialogTitle>
        <DialogDescription data-testid="execution-line">{EXECUTION_LINE}</DialogDescription>
      </DialogHeader>
      <form className="grid grid-cols-[110px_minmax(0,1fr)] items-center gap-x-3 gap-y-2 text-xs" onSubmit={(e) => { e.preventDefault(); if (!badModel && !badLabel) void preview(form) }}>
        <Label htmlFor="agent-harness" className="text-xs text-text-muted">Agent</Label>
        <select id="agent-harness" className="h-7 rounded border border-border bg-bg px-2" value={harness} onChange={(e) => setHarness(e.target.value as Harness)}>
          {HARNESSES.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
        </select>
        <Label htmlFor="agent-mode" className="text-xs text-text-muted">Mode</Label>
        <select id="agent-mode" className="h-7 rounded border border-border bg-bg px-2" value={mode} onChange={(e) => setMode(e.target.value as AgentMode)} aria-describedby="agent-mode-help">
          {AGENT_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
        <span />
        <p id="agent-mode-help" data-testid="agent-mode-help" className="-mt-1 text-text-muted">{MODE_HELP[mode]}</p>
        <Label htmlFor="agent-model" className="text-xs text-text-muted">Model</Label>
        <Input id="agent-model" className="h-7 text-xs" placeholder="the agent's default" value={model} autoComplete="off" spellCheck={false} onChange={(e) => setModel(e.target.value)} />
        {harness === 'claude-code' && (<>
          <Label htmlFor="agent-subagent" className="text-xs text-text-muted">Subagent model</Label>
          <Input id="agent-subagent" className="h-7 text-xs" placeholder="the agent's default" value={subagent} autoComplete="off" spellCheck={false} onChange={(e) => setSubagent(e.target.value)} />
        </>)}
        <Label htmlFor="agent-label" className="text-xs text-text-muted">Label</Label>
        <Input id="agent-label" className="h-7 text-xs" placeholder="optional, up to 40 characters" value={label} maxLength={40} autoComplete="off" onChange={(e) => setLabel(e.target.value)} />
        <span className="text-text-muted">Worktree</span>
        <label className="flex items-center gap-2"><input type="checkbox" checked={worktree} onChange={(e) => setWorktree(e.target.checked)} /> Work in the ticket's own worktree when it has a branch</label>
        {badModel && <p role="alert" className="col-span-2 text-danger">A model is a name of letters, digits and . _ : / - (no spaces, not starting with -).</p>}
        {stale && shown && <p className="col-span-2 text-text-muted" data-testid="agent-stale">The choices changed: preview again before you start.</p>}
        <div className="col-span-2 flex justify-end">
          <Button type="submit" size="xs" variant="outline" disabled={loading || !!badModel || badLabel}>{loading ? 'Previewing…' : 'Preview'}</Button>
        </div>
      </form>

      {loading && !data && <Skeleton className="h-24" />}
      {data && <PreviewLines data={data} ticket={ticket} />}
      {note && <p role="status" className="text-xs text-warning" data-testid="agent-changed">{note}</p>}
      {data?.blocked && !noGrant && (
        <div role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-xs text-danger" data-testid="agent-blocked">
          <p>Cannot start: {blockedText(data.blocked) ?? 'the host refused.'}{data.blocked.code && <span className="ml-1 font-mono text-[11px]">({data.blocked.code})</span>}</p>
          {data.blocked.message && <p className="mt-1 text-text-muted">The host says: <Prose inline>{data.blocked.message}</Prose></p>}
        </div>
      )}
      <RunnerGrantCard initial={grantStep} ws={ws} runner={runner.data?.runner ?? null} loading={runner.isPending} blocked={noGrant ? data?.blocked ?? null : null}
        onChanged={() => { void runner.refetch(); void preview(shown?.args ?? form) }} />
      {unreadable && !data?.blocked && <p role="alert" className="text-xs text-danger" data-testid="agent-unreadable">The command or the folder cannot be shown here, so it cannot be started from the dashboard. Fix it in the terminal (runner.json or the path), then preview again.</p>}
      {error && <p role="alert" className="text-xs text-danger" data-testid="agent-error"><Inline>{error}</Inline></p>}
      <DialogFooter>
        <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
        <Button size="sm" disabled={!data || !data.spec || !!data.blocked || unreadable || stale || busy || loading} onClick={() => void start()} data-testid="agent-start">
          {busy ? <><Loader2 className="size-4 animate-spin" aria-hidden /> Starting…</> : 'Start agent'}
        </Button>
      </DialogFooter>
    </>
  )
}

/** The host's preview, shown exactly: what runs, where (and whether a worktree is created first), env names, grant. */
function PreviewLines({ data, ticket }: { data: AgentPreview; ticket?: string }) {
  const where = data.cwd_path !== null ? data.cwd_path : null
  const creates = data.will_create_worktree && data.cwd.kind === 'worktree' && where !== null
  return (
    <div className="space-y-2 text-xs" data-testid="agent-preview">
      {data.grant && (
        <dl className="grid grid-cols-[110px_minmax(0,1fr)] gap-x-3 gap-y-1.5">
          <dt className="text-text-muted">Runner grant</dt><dd data-testid="agent-grant"><Raw>{data.grant.id}</Raw>, {data.grant.scope} tickets, until {clock(data.grant.until)}</dd>
        </dl>
      )}
      <details data-testid="agent-details">
        <summary className="cursor-pointer text-text-muted">Details</summary>
        <dl className="mt-2 grid grid-cols-[110px_minmax(0,1fr)] gap-x-3 gap-y-1.5">
          <dt className="text-text-muted">Runs</dt>
          <dd>{data.display !== null ? <Raw>{data.display}</Raw> : <span className="text-text-muted">{data.display_hidden ? 'Hidden: the command has invisible characters' : 'Not shown'}</span>}</dd>
          <dt className="text-text-muted">In</dt>
          <dd data-testid="agent-cwd">
            {creates ? (
              <Raw>{worktreeLine(data.cwd.ticket ?? data.ticket ?? ticket ?? '', data.cwd.repo ?? '', where)}</Raw>
            ) : (
              <>
                <span className="block">{data.cwd.kind === 'repo' ? SHARED_COPY : cwdText(data)}</span>
                {where !== null ? <Raw>{where}</Raw> : <span className="text-text-muted">{data.cwd_path_hidden ? 'Hidden: the path has invisible characters' : ''}</span>}
              </>
            )}
          </dd>
          <dt className="text-text-muted">As</dt>
          <dd>an agent with your runner grant, not as you</dd>
          {data.env.length > 0 && (<><dt className="text-text-muted">Environment</dt><dd className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px]">{data.env.map((n) => <span key={n}><Inline>{n}</Inline></span>)}</dd></>)}
        </dl>
      </details>
      {data.warnings.map((w, i) => <p key={i} role="note" className="text-warning"><Prose inline>{w}</Prose></p>)}
    </div>
  )
}

export type GrantStep =
  | { kind: 'idle' }
  | { kind: 'polling'; depositId: string | null; grant: string | null }
  | { kind: 'confirmed'; until: string | null }
  | { kind: 'rejected'; reason: string; grant: string | null }
  | { kind: 'unknown'; grant: string | null }
  | { kind: 'refused'; text: string }

/**
 * The runner grant card: the owner's risk sentence (verbatim), the person's current grant with Forget, or "Issue runner
 * grant" (hours up to the workspace setting, at most 168, scope) as a click request with the typed word. Its outcome comes from the signer's answer:
 * `confirmed`, `rejected` (a closed reason) or `unknown` (asked again a few times, then what to do).
 */
export function RunnerGrantCard({ ws, runner, loading, blocked, onChanged, initial, extend, onOutcome }: { extend?: { id: string; scope: 'all' | 'workable'; until: string }; onOutcome?: () => void; initial?: GrantStep; ws: string; runner: Runner | null; loading: boolean; blocked: AgentPreview['blocked']; onChanged: () => void }) {
  const workspaces = useQuery(queries.workspaces())
  const workspace = workspaces.data?.find(w => w.id === ws)
  // 8 h / 1 / 3 / 7 days, never above the workspace maximum; an Extend never offers less than the access it replaces.
  // The maximum is the SIGNED setting (`settings.grant_hours`, from the host's settings read): the workspace list does not carry it.
  const hostSettings = useQuery({ ...queries.hostSettings(ws), retry: false })
  const maxHours = grantDefaultHours({ grant_hours: hostSettings.data?.grant_hours ?? workspace?.grant_hours })
  const lengths = runnerLengths(maxHours, extend ? Date.parse(extend.until) - nowMs() : 0)
  const [selectedHours, setHours] = useState(8)
  const hours = lengths.some(([h]) => h === selectedHours) ? selectedHours : (lengths[0]?.[0] ?? 0)
  const [scope, setScope] = useState<'workable' | 'all'>(extend?.scope ?? 'workable')
  const [step, setStep] = useState<GrantStep>(initial ?? { kind: 'idle' })
  const [forgetting, setForgetting] = useState<'ask' | 'busy' | null>(null)
  const [forgetIdem, setForgetIdem] = useState(newIdem)
  const [error, setError] = useState('')
  const click = useClickSign(ws)
  const revoke = useClickSign(ws)
  // One click only where this workspace really uses it AND the person's method is click; otherwise the same terminal
  // hand-off as every other signature. The custody itself is still checked by core.
  const methods = useSignMethods(ws)
  const oneClick = clickReady(workspace) && !!methods.allowed?.includes('click')
    && (methods.preference?.method ? methods.preference.method === 'click' : true)
  const [handoff, setHandoff] = useState(false)
  const changed = useRef(onChanged)
  changed.current = onChanged
  const outcome = useRef(onOutcome)
  outcome.current = onOutcome
  useEffect(() => {
    if (['polling', 'confirmed', 'rejected', 'unknown'].includes(step.kind)) outcome.current?.()
  }, [step.kind])
  const grant = runner?.grant ?? null
  const enabled = runner?.enabled === true && !!workspace
  const action: ClickAction = { action: 'runner_grant', hours, scope, ...(extend ? { extend: extend.id } : {}) }

  // A signed grant: what the signer says happened to the secret.
  useEffect(() => {
    const signed = click.signed
    if (click.phase !== 'done' || !signed) return
    if (signed.deposit === 'confirmed') { setStep({ kind: 'confirmed', until: null }); changed.current() }
    else if (signed.deposit === 'rejected') setStep({ kind: 'rejected', reason: depositReason(signed.deposit_reason), grant: signed.grant ?? null })
    // `unknown`, or a lost sign answer the hook settled from the host's state (no deposit fields at all, review L1): the
    // deposit id is the request's ULID either way, so the host can still be asked where the secret went.
    else setStep({ kind: 'polling', depositId: signed.deposit_id ?? depositIdOf(click.prepared?.request_id), grant: signed.grant ?? null })
  }, [click.phase, click.signed, click.prepared])

  // `unknown`: ask where the deposit stands a few times, bounded; then say what to do.
  useEffect(() => {
    if (step.kind !== 'polling') return
    const { depositId: id, grant: gid } = step
    let stopped = false
    let n = 0
    const tick = async () => {
      if (stopped) return
      n += 1
      let got: string | undefined
      let reason: string | undefined
      try {
        if (id) ({ deposit: got, reason } = await agentsApi.depositStatus(ws, id))
      } catch { /* still unknown */ }
      if (stopped) return
      if (got === 'confirmed') { setStep({ kind: 'confirmed', until: null }); changed.current(); return }
      if (got === 'rejected') { setStep({ kind: 'rejected', reason: depositReason(reason), grant: gid ?? null }); return }
      if (n >= DEPOSIT_POLLS || !id) { setStep({ kind: 'unknown', grant: gid ?? null }); return }
      timer = setTimeout(() => void tick(), DEPOSIT_POLL_MS)
    }
    let timer = setTimeout(() => void tick(), 0)
    return () => { stopped = true; clearTimeout(timer) }
  }, [step, ws])

  // A refusal before anything was signed: terminal only (execution off, longer than the workspace allows), an older orch, click off.
  useEffect(() => {
    if (click.phase !== 'failed' || !click.problem) return
    setStep({ kind: 'refused', text: refusalText(click.error) ?? click.problem.body })
  }, [click.phase, click.problem, click.error])

  const issue = async () => {
    if (!enabled) return
    setError('')
    setStep({ kind: 'idle' })
    if (!oneClick) { setHandoff(true); return }
    // "Issue runner grant" again after a refusal is an explicit new attempt (the hook advances the intent's counter).
    if (click.phase === 'failed' || click.phase === 'cancelled') await click.again(action)
    else await click.start(action)
  }
  const forget = async () => {
    if (!grant) return
    setForgetting('busy')
    setError('')
    try {
      await agentsApi.forget(ws, grant.id, forgetIdem)
      setForgetIdem(newIdem())
      setForgetting(null)
      changed.current()
    } catch (e) {
      setForgetting('ask')
      setError(message(e))
    }
  }

  const view = click.view
  const viewOk = view ? depositViewOk(view.view, ws) : false
  return (
    <section aria-label="Runner grant" data-testid="runner-grant-card" className="space-y-2 rounded-md border border-border bg-bg px-3 py-2 text-xs">
      <p className="flex items-center gap-2 font-medium text-text"><KeyRound className="size-3.5 text-text-muted" aria-hidden />Runner grant</p>
      <p role="note" data-testid="runner-grant-risk" className="text-warning">{DEPOSIT_RISK}</p>
      {loading && <Skeleton className="h-6" />}
      {grant && !extend && step.kind !== 'polling' && (
        <div className="flex flex-wrap items-center gap-2" data-testid="runner-grant-current">
          <span>Your runner holds a grant for {grant.scope} tickets until <time dateTime={grant.until}>{clock(grant.until)}</time>.</span>
          {forgetting === null && <Button size="xs" variant="ghost" onClick={() => setForgetting('ask')}>Forget runner grant</Button>}
          {forgetting !== null && (
            <span className="flex flex-wrap items-center gap-2" role="group" aria-label="Forget the runner grant">
              <span>Forget the runner grant on this machine? Running agents keep theirs; new agents need a new grant.</span>
              <Button size="xs" variant="outline" disabled={forgetting === 'busy'} onClick={() => void forget()}>Forget</Button>
              <Button size="xs" variant="ghost" disabled={forgetting === 'busy'} onClick={() => setForgetting(null)}>Keep</Button>
            </span>
          )}
        </div>
      )}
      {extend && <p>Agents that are running now stop; new agents use the extended access.</p>}
      {blocked && <p className="text-text" data-testid="runner-grant-needed">{blockedText(blocked)}</p>}
      {(!grant || extend) && !loading && step.kind !== 'confirmed' && (click.phase === 'idle' || click.phase === 'failed' || click.phase === 'cancelled') && (
        lengths.length === 0 ? <p className="text-text-muted" data-testid="runner-grant-longest">Your access already lasts as long as this workspace allows ({hoursText(maxHours)}), so there is nothing to extend. Whoever owns the workspace can allow longer in Settings, General.</p> : <div className="flex flex-wrap items-center gap-2">
          <Label htmlFor="grant-hours" className="text-xs text-text-muted">Length</Label>
          <select id="grant-hours" className="h-7 rounded border border-border bg-bg px-2" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {lengths.map(([h, label]) => <option key={h} value={h}>{label}</option>)}
          </select>
          <Label htmlFor="grant-scope" className="text-xs text-text-muted">Covers</Label>
          <select id="grant-scope" disabled={!!extend} className="h-7 rounded border border-border bg-bg px-2" value={scope} onChange={(e) => setScope(e.target.value as 'workable' | 'all')}>
            <option value="workable">Tickets you may work on</option>
            <option value="all">All tickets</option>
          </select>
          <Button size="xs" disabled={!enabled || methods.loading} onClick={() => void issue()} data-testid="runner-grant-issue">{extend ? 'Extend access' : 'Issue runner grant'}</Button>
          {!runner?.enabled && runner && <span className="text-text-muted" data-testid="runner-grant-off">{TERMINAL_ONLY}</span>}
        </div>
      )}
      {handoff && workspace && action.hours > 0 && <HandoffDialog ws={ws} title={extend ? 'Extend agent access' : 'Issue runner grant'} action={action} autoStart
        summary={<p>Agents on this machine can act for you on {scope === 'all' ? 'all tickets' : 'the tickets you may work on'} for {hoursText(hours)}. When you sign in the terminal, choose to hand the grant to the orch runner.</p>}
        onSettled={(phase, _state, request) => {
          if (phase !== 'done') return
          setHandoff(false)
          setStep({ kind: 'polling', depositId: depositIdOf(request?.id), grant: null })
        }} onClose={() => setHandoff(false)} />}
      {(click.phase === 'preparing' || click.phase === 'viewing') && <Skeleton className="h-16" />}
      {view && (click.phase === 'ready' || click.phase === 'signing') && (viewOk ? (
        <ClickSignPanel ws={ws} answer={view} problem={click.problem} signing={click.phase === 'signing'} onSign={(w) => void click.sign(w)} onCancel={() => void click.cancel().then(() => click.reset(true))} />
      ) : (
        <p role="alert" className="text-danger" data-testid="runner-grant-bad-view">The review does not say where the grant goes. Nothing was signed. <Button size="xs" variant="ghost" onClick={() => void click.cancel().then(() => click.reset(true))}>Cancel</Button></p>
      ))}
      {click.phase === 'settling' && <p role="status" className="text-text-muted">{click.settleNote ?? 'The signature is settling…'}</p>}
      {step.kind === 'polling' && <p role="status" className="text-text-muted" data-testid="runner-grant-polling">Signed. Asking the runner whether it holds the grant…</p>}
      {step.kind === 'confirmed' && <p role="status" className="text-success" data-testid="runner-grant-confirmed">Your runner holds the grant{grant ? <> until <time dateTime={grant.until}>{clock(grant.until)}</time></> : ''}.</p>}
      {step.kind === 'rejected' && (
        <div role="alert" className="text-danger" data-testid="runner-grant-rejected">
          <p>The runner did not take the grant: {step.reason}</p>
          <p className="text-text-muted">The grant stays issued{step.grant ? <> (<Raw>{step.grant}</Raw>)</> : ''}: revoke it, then issue a new one.</p>
          {step.grant && <RevokeStep grant={step.grant} click={revoke} />}
        </div>
      )}
      {step.kind === 'unknown' && (
        <div role="alert" className="text-warning" data-testid="runner-grant-unknown">
          <p>{DEPOSIT_UNKNOWN}</p>
          {step.grant ? <RevokeStep grant={step.grant} click={revoke} /> : <p className="text-text-muted" data-testid="runner-grant-find">{FIND_GRANT}</p>}
        </div>
      )}
      {step.kind === 'refused' && <p role="alert" className="text-danger" data-testid="runner-grant-refused">{step.text}</p>}
      {error && <p role="alert" className="text-danger"><Inline>{error}</Inline></p>}
    </section>
  )
}

const hoursText = (h: number) => h >= 24 && h % 24 === 0 ? `${h / 24} ${h === 24 ? 'day' : 'days'}` : `${h} hours`

/** Revoke the grant (a click request of the existing `grant_revoke` action), when its deposit failed or is unknown. */
function RevokeStep({ grant, click }: { grant: string; click: ReturnType<typeof useClickSign> }) {
  const action: ClickAction = { action: 'grant_revoke', grant }
  if (click.phase === 'done') return <p className="text-text">Revoked.</p>
  if (click.view && (click.phase === 'ready' || click.phase === 'signing'))
    return <ClickSignPanel answer={click.view} problem={click.problem} signing={click.phase === 'signing'} onSign={(w) => void click.sign(w)} onCancel={() => void click.cancel().then(() => click.reset(true))} />
  return (
    <div className="mt-1 flex flex-wrap items-center gap-2">
      <Button size="xs" variant="outline" disabled={click.phase !== 'idle' && click.phase !== 'failed'} onClick={() => void (click.phase === 'failed' ? click.again(action) : click.start(action))}>Revoke grant</Button>
      {click.problem && <span className="text-text-muted">{click.problem.body}</span>}
    </div>
  )
}

/** The deposit id of a click request: its ULID (`hr_` stripped), or null when it is not one. */
export const depositIdOf = (requestId: string | undefined | null): string | null =>
  requestId && /^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$/.test(requestId) ? requestId.slice(3) : null

/** When the grant's id is not known here (a lost sign answer): where to find it. */
export const FIND_GRANT = 'Its id did not reach this page: revoke your newest runner grant under Settings → Grants, or run `orch grant --runner` in your terminal for a new one.'

/** The grant's refusals before signing, in the approved words. */
export function refusalText(e: unknown): string | null {
  if (!(e instanceof ApiError)) return null
  if (e.code === 'human.click_refused' && e.reason === 'terminal_only') return e.why === 'update_orch' ? UPDATE_ORCH : TERMINAL_ONLY
  if (e.code === 'human.click_refused' && e.reason === 'disabled') return TERMINAL_GRANT // review I4: click is off here
  if (e.code === 'sign.click_only' || e.code === 'click.unavailable' || e.code === 'click.contract' || e.code === 'sign.convenience_unavailable') return TERMINAL_GRANT
  return null
}
