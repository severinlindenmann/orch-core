// Start agent on a real host (M7b, contract v1.6 sections 12.3 and 12.4). Core's own dialog, never an addon frame: the
// host's preview first (what runs, where, with which runner grant, what blocks it), then an explicit Start that posts
// exactly the previewed values plus the preview's `spec`, with one idem per dialog opening (a retry returns the same
// run, never a second process). `launch.changed` previews again and asks for a new Start. Every host string is
// untrusted text (visible.tsx). The runner grant is the only signed step: a click request whose secret goes from the
// signer to the runner, never through this page.

import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound } from 'lucide-react'
import { toast } from 'sonner'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  AGENT_MODES, agentsApi, blockedText, HARNESSES, cwdText, DEPOSIT_UNKNOWN, depositReason, runnerQuery, SHARED_COPY, TERMINAL_GRANT,
  TERMINAL_ONLY, LOGIN_NOTICE, worktreeLine, type AgentArgs, type AgentMode, type AgentPreview, type Harness, type Runner,
} from '@/api/agents'
import { clickReady, DEPOSIT_RISK, depositViewOk, type ClickAction } from '@/api/click'
import { newIdem, type Run } from '@/api/runs'
import { ApiError } from '@/api/types'
import { ClickSignPanel } from '@/components/sign/ClickSignPanel'
import { storyOf } from '@/components/sign/story'
import { SignPopover } from '@/components/sign/SignPopover'
import { useClickSign } from '@/components/sign/useClickSign'
import { Inline, Prose, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { queries } from '@/api/queries'
import { hasFeature } from '@/api/features'
import { modelLabel, routingQueries, STRONG_MODELS } from '@/api/modelRouting'
import { grantDefaultHours, runnerLengths } from '@/api/grants'
import { fmtDateTime, fmtExact, nowMs } from '@/lib/time'
import { EXECUTION_LINE } from './executionLine'
import { MODE_LABEL, ModelPick } from '@/components/agents/AgentChoice'
import { AgentIcon } from '@/components/agents/AgentIcon'
import { AccessSelect } from '@/components/sign/AccessSelect'
import { SignButton } from '@/components/sign/SignButton'
import { lastTrigger, SignShell } from '@/components/sign/SignShell'
import { RunnerSwitch } from '@/components/RunnerSwitch'
import { usableGrant } from '@/app/pages/new-ticket/startAfterCreate'

export { MODE_HELP, MODE_LABEL } from '@/components/agents/AgentChoice'

const DEPOSIT_POLLS = 5
const DEPOSIT_POLL_MS = 2000

const same = (a: AgentArgs, b: AgentArgs) => JSON.stringify(a) === JSON.stringify(b)
const message = (e: unknown) => (e instanceof ApiError ? e.message : 'The dashboard server did not answer.')
const clock = fmtDateTime

/** The form's values as `agent.preview`'s arguments: empty optionals left out; the subagent model is Claude Code's only. */
function argsOf(f: { ticket?: string; harness: Harness; mode: AgentMode; model: string; subagent: string; label: string; worktree: boolean; delegate: boolean; maxChildren: number }): AgentArgs {
  return {
    ...(f.ticket ? { ticket: f.ticket } : {}),
    harness: f.harness,
    mode: f.mode,
    ...(f.model ? { model: f.model } : {}),
    ...(f.harness === 'claude-code' && f.subagent ? { subagent_model: f.subagent } : {}),
    ...(f.label ? { label: f.label } : {}),
    worktree: f.worktree,
    // P2-3: Codex has no delegation limits to set; Claude Code gets the policy of the block (off unless asked)
    ...(f.harness === 'claude-code' ? { policy: { delegate: f.delegate, max_children: f.maxChildren } } : {}),
  }
}

/** What a caller prefilled (the New ticket form after "Create and start agent"): the choice and one plain sentence saying why the person is asked. */
export interface StartInitial {
  harness?: Harness
  mode?: AgentMode
  model?: string
  note?: string
  /** The idem of an earlier Start whose answer was lost: a retry then returns that run, never a second one. */
  idem?: string
}

const lastKey = (ws: string) => `orch.dashboard.start-agent.harness.${ws}`
export function lastHarness(ws: string): Harness {
  try {
    const v = localStorage.getItem(lastKey(ws))
    return HARNESSES.some((h) => h.id === v) ? (v as Harness) : 'claude-code'
  } catch {
    return 'claude-code'
  }
}
function rememberHarness(ws: string, h: Harness) {
  try { localStorage.setItem(lastKey(ws), h) } catch { /* not remembered */ }
}

/**
 * Start agent (run 8, V2). With valid access there is NO dialog: the host previews, and when nothing needs reading the
 * agent starts and one toast says so. The dialog (a popover next to the button, a sheet on phones) opens only when
 * something needs the person: first-start notice, missing or short access (the access length with the signature, the typed word
 * where the host's review asks for it), a blocked reason, a failed start, or `choose` ("Change agent or model").
 */
export function StartAgentDialog({ ws, ticket, open, onClose, onStarted, initial, choose }: { ws: string; ticket?: string; open: boolean; onClose: () => void; onStarted?: (run: Run) => void; initial?: StartInitial; /** Show the form even when the agent could start right away. */ choose?: boolean }) {
  if (!open) return null
  return <StartAgent ws={ws} ticket={ticket} onClose={onClose} onStarted={onStarted} initial={initial} choose={choose} />
}

type Step = 'form' | 'sign' | 'starting'

function StartAgent({ ws, ticket, onClose, onStarted, initial, choose }: { ws: string; ticket?: string; onClose: () => void; onStarted?: (run: Run) => void; initial?: StartInitial; choose?: boolean }) {
  const qc = useQueryClient()
  const [anchor] = useState<HTMLElement | null>(() => lastTrigger())
  // One idem per opening, kept for every retry of Start (the run id is `r_` + its ULID).
  const [idem] = useState(() => initial?.idem ?? newIdem())
  const [harness, setHarness] = useState<Harness>(initial?.harness ?? lastHarness(ws))
  const [mode, setMode] = useState<AgentMode>(initial?.mode ?? 'work')
  const [model, setModel] = useState(initial?.model ?? '')
  const [subagent, setSubagent] = useState('')
  const [label, setLabel] = useState('')
  const [worktree, setWorktree] = useState(true)
  const [delegate, setDelegate] = useState(false)
  const [maxChildren, setMaxChildren] = useState(3)
  const [moreOpen, setMoreOpen] = useState(!!initial?.model)
  const form = argsOf({ ticket, harness, mode, model, subagent, label, worktree, delegate, maxChildren })
  const [shown, setShown] = useState<{ args: AgentArgs; data: AgentPreview } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const reason = initial?.note ?? ''
  const [busy, setBusy] = useState(false)
  const [step, setStep] = useState<Step>('form')
  const [signing, setSigning] = useState(false)
  const asked = useRef(0)
  const runner = useQuery({ ...runnerQuery(ws) })
  const workspaces = useQuery(queries.workspaces())
  const prefix = workspaces.data?.find((w) => w.id === ws)?.prefix ?? ''
  // Model routing: the closed list of models for this agent (from core); the choice is a name of that list, never free text.
  const routing = hasFeature('model_routing', ws)
  const choices = useQuery({ ...routingQueries.choices(ws), enabled: routing })
  const models = choices.data?.harnesses?.find((h) => h.id === harness)?.models ?? []
  const failing = useQuery({ ...queries.ticket(ws, ticket ?? ''), enabled: routing && !!ticket })
  const strong = (STRONG_MODELS[harness] ?? []).find((id) => models.some((m) => m.id === id))
  const checkFailing = !!failing.data?.tasks_state?.some((t) => t.state !== 'done' && t.state !== 'skipped' && t.receipt !== null && t.receipt.exit !== 0)
  const agentName = HARNESSES.find((h) => h.id === harness)?.name ?? harness

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
  // The host is asked again by itself a moment after a choice changes (no "check" button); Start stays off meanwhile.
  const wanted = JSON.stringify(form)
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; void preview(form); return }
    if (badModel || badLabel) return
    const t = setTimeout(() => void preview(form), 350)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted])

  const badModel = (model && !models.some((m) => m.id === model)) || (subagent && harness === 'claude-code' && !models.some((m) => m.id === subagent))
  const badLabel = label.length > 40
  const stale = !shown || !same(shown.args, form)
  const data = shown?.data
  // The risk notice is shown in full until it is acknowledged once (kept by the dashboard server, per person and
  // workspace); afterwards it is one line whose Details hold the same words. A failed read counts as not acknowledged.
  const ack = useQuery({ queryKey: ['risk-notices', ws], queryFn: () => agentsApi.riskNotices(ws), retry: false })
  const startFull = ack.data?.acknowledged.start_agent !== true
  const warnings = data?.warnings ?? []
  const loginNotice = warnings.filter(isLoginNotice)
  const otherWarnings = warnings.filter((x) => !isLoginNotice(x))
  const loginFull = loginNotice.length > 0 && ack.data?.acknowledged.login_shell !== true
  const acknowledge = useCallback(async (notices: ('start_agent' | 'login_shell')[]) => {
    for (const n of notices) {
      try {
        qc.setQueryData(['risk-notices', ws], await agentsApi.acknowledge(ws, n))
      } catch { /* not saved: the full notice shows again next time */ }
    }
  }, [qc, ws])
  const shownFull = [...(startFull ? ['start_agent' as const] : []), ...(loginFull ? ['login_shell' as const] : [])]
  const ackNow = useRef<{ login_shell?: boolean }>({})
  ackNow.current = ack.data?.acknowledged ?? {}
  const ackRef = useRef({ shownFull, acknowledge })
  ackRef.current = { shownFull, acknowledge }

  const noGrant = data?.blocked?.code === 'launch.no_grant'
  // Review L2: nothing starts that the person could not read (a command or a folder withheld for invisible characters).
  const unreadable = !!data && (data.display_hidden || data.cwd_path_hidden || data.display === null || data.cwd_path === null)
  const ready = !badModel && !stale && !loading && !note && !!data && !data.blocked && !!data.spec && !unreadable

  const info = runner.data?.runner ?? null
  const startable = usableGrant(info, nowMs())
  const runnerOff = !!info && (!info.enabled || data?.blocked?.code === 'runner.disabled')
  const needsAccess = !runner.isPending && !!info && info.enabled && (!startable || noGrant)

  const toastId = `start-agent-${ticket ?? 'any'}`
  /** Start with exactly the previewed values. Returns false (and says why on the form) when it did not start. */
  const start = async (from?: { args: AgentArgs; data: AgentPreview }): Promise<boolean> => {
    const at = from ?? shown
    if (!at || !at.data.spec || at.data.blocked || busy) return false
    setBusy(true)
    setError('')
    setNote('')
    try {
      const { run } = await agentsApi.start(ws, at.args, at.data.spec, idem)
      if (ackRef.current.shownFull.length) void ackRef.current.acknowledge(ackRef.current.shownFull) // starting with the full notice on screen counts as reading it
      rememberHarness(ws, at.args.harness)
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
      toast.success(`${agentName} started${ticket ? ` on ${ticket}` : ''}`, { id: toastId })
      onStarted?.(run)
      onClose()
      return true
    } catch (e) {
      toast.dismiss(toastId)
      if (e instanceof ApiError && e.code === 'launch.changed') {
        // What would run changed since the preview (also a new runner grant): show it again, ask for a new Start.
        setNote('What would run changed since the last check. It has been checked again: read it, then start.')
        await preview(at.args)
      } else {
        // The same idem stays: Start again returns the recorded run, never a second one. A lost answer: read the list.
        if (!(e instanceof ApiError) || e.status >= 500) void qc.invalidateQueries({ queryKey: ['runs', ws] })
        setError(message(e))
      }
      return false
    } finally {
      setBusy(false)
      setStep('form')
    }
  }

  // Instant: valid access and nothing to read means no dialog at all. Anything else falls through to the form.
  const instantAllowed = !choose && !initial && (runner.isPending || !!startable)
  const [fellThrough, setFellThrough] = useState(false)
  // Only a dialog that has not left the form: once the access is being signed, the grant that lands (and makes the access
  // valid) must not turn the dialog silent and remount the review under the person's hands.
  const leftForm = useRef(false)
  if (step !== 'form') leftForm.current = true
  const quiet = instantAllowed && !fellThrough && !leftForm.current
  const tried = useRef(false)
  const settled = !runner.isPending && !ack.isPending && !!data && !stale && !loading
  useEffect(() => {
    if (!quiet || !settled || tried.current) return
    tried.current = true
    const fine = ready && !startFull && !loginFull && otherWarnings.length === 0 && !!shown
    if (!fine) { setFellThrough(true); return }
    toast.loading(`Starting ${agentName}…`, { id: toastId })
    void start(shown!).then((ok) => { if (!ok) setFellThrough(true) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quiet, settled])
  // "Still starting…" when the host takes longer than 8 s.
  useEffect(() => {
    if (!busy) return
    const t = setTimeout(() => toast.loading('Still starting…', { id: toastId }), 8000)
    return () => clearTimeout(t)
  }, [busy, toastId])
  // A start that never reached the host (nothing started) must not leave its progress toast behind.
  useEffect(() => () => { if (!tried.current) toast.dismiss(toastId) }, [toastId])

  // Access: the length is signed together with the grant; after the signature the agent starts by itself.
  const settings = useQuery({ ...queries.hostSettings(ws), retry: false })
  const cap = grantDefaultHours({ grant_hours: settings.data?.grant_hours ?? workspaces.data?.find((w) => w.id === ws)?.grant_hours })
  const lengths = runnerLengths(cap)
  const [hoursChoice, setHours] = useState(8)
  const hours = lengths.some(([h]) => h === hoursChoice) ? hoursChoice : (lengths[0]?.[0] ?? 8)
  const afterGrant = async (deposit: string | undefined) => {
    setStep('starting')
    // The runner holds the grant once the deposit is confirmed; a terminal signature settles a little later: read again a few times.
    let args = shown?.args ?? form
    for (let i = 0; i < (deposit === 'confirmed' ? 1 : DEPOSIT_POLLS); i++) {
      const r = (await runner.refetch()).data?.runner ?? null
      if (usableGrant(r, nowMs())) {
        try {
          const data2 = await agentsApi.preview(ws, args)
          const before = shown?.data ?? null
          setShown({ args, data: data2 })
          // Review L2: start by itself only what the person saw (same command, folder, model, no new worktree) and what the
          // instant path would start (nothing to read first). Anything else goes back to the form for a press on Start.
          const loginOpen = data2.warnings.some(isLoginNotice) && ackNow.current.login_shell !== true
          const fine = !data2.blocked && !!data2.spec && !unreadableOf(data2) && !loginOpen && !data2.warnings.some((x) => !isLoginNotice(x))
          if (fine && before && sameShown(before, data2)) { await start({ args, data: data2 }); return }
          if (!data2.blocked) { setNote(CHANGED_NOTE); toast.dismiss(toastId); setStep('form'); return }
        } catch (e) { setError(message(e)) }
        break
      }
      if (deposit === 'rejected') break
      await new Promise((r2) => setTimeout(r2, DEPOSIT_POLL_MS))
    }
    toast.dismiss(toastId)
    setStep('form')
    setError((e) => e || 'The access was signed, but the agent runner did not take it. Try again.')
  }

  const title = `Start ${agentName}${ticket ? ` on ${ticket}` : ''}`
  const accessSelect = <AccessSelect hours={hours} onHours={setHours} lengths={lengths} prefix={prefix} agentName={agentName} />

  if (quiet) return null
  if (step === 'sign') {
    return (
      <SignPopover
        key={hours}
        ws={ws}
        title="Give agents access"
        action={{ action: 'runner_grant', hours, scope: 'workable' }}
        startsAgent
        anchor={anchor}
        controls={accessSelect}
        doneToast=""
        onSigning={setSigning}
        onSettled={(phase, state) => { if (phase === 'done') void afterGrant(state?.deposit) }}
        onClose={() => setStep((s) => (s === 'sign' ? 'form' : s))}
      />
    )
  }

  const statusLine = (
    <div className="space-y-1 text-xs leading-5" data-testid="agent-status">
      {badModel && <p role="alert" className="text-danger">Choose a model from the list.</p>}
      {!badModel && !stale && !loading && note && <p role="status" className="text-warning" data-testid="agent-changed">{note}</p>}
      {!stale && !loading && data?.blocked && !noGrant && !runnerOff && (
        <div role="alert" className="text-danger" data-testid="agent-blocked">
          <p>{blockedText(data.blocked) ?? 'The host would not start it.'}</p>
          {data.blocked.message && <p className="mt-1 text-text-muted">The host says: <Prose inline>{data.blocked.message}</Prose></p>}
        </div>
      )}
      {unreadable && !data?.blocked && <p role="alert" className="text-danger" data-testid="agent-unreadable">The command or the folder cannot be shown here, so it cannot be started from the dashboard. Fix it in the terminal (runner.json or the path), then press Start again.</p>}
      {!stale && otherWarnings.map((x, i) => <p key={i} role="note" className="text-warning"><Prose inline>{x}</Prose></p>)}
      {error && <p role="alert" className="text-danger" data-testid="agent-error"><Inline>{error}</Inline></p>}
    </div>
  )
  const primaryDisabled = !data || !data.spec || !!data.blocked || unreadable || stale || busy || loading || ack.isPending
  const primary = needsAccess
    ? { label: 'Sign and start agent', go: () => setStep('sign'), disabled: runner.isPending || (!data && !noGrant) || stale || loading }
    : { label: 'Start agent', go: () => void start(), disabled: primaryDisabled }

  return (
    <SignShell
      anchor={anchor}
      open={!signing}
      onDismiss={() => !busy && onClose()}
      title={title}
      description={<span data-testid="agent-sentence">{MODE_SENTENCE(agentName)[mode]}</span>}
      showClose
      onOpenAutoFocus={(e) => e.preventDefault()}
    >
      <div data-start-agent className="space-y-3 text-xs leading-5">
        {reason && !ready && <p role="note" className="text-warning" data-testid="agent-initial-note"><Inline>{reason}</Inline></p>}
        {reason && ready && <p role="status" className="text-success" data-testid="agent-ready">{ticket ?? 'The ticket'} is created. Check the details, then press Start agent.</p>}
        <div role="radiogroup" aria-label="Agent" className="flex flex-wrap gap-2">
          {HARNESSES.map((h) => (
            <button key={h.id} type="button" role="radio" aria-checked={h.id === harness} onClick={() => { setHarness(h.id); setModel('') }}
              className={`inline-flex min-h-9 items-center gap-2 rounded-md border px-2.5 text-[13px] ${h.id === harness ? 'border-brand bg-brand-soft text-text' : 'border-border text-text-muted hover:text-text'}`}>
              <AgentIcon harness={h.id} size={18} />{h.name}
            </button>
          ))}
        </div>
        {statusLine}
        {!stale && !loading && runnerOff && (
          <div data-testid="agent-runner-off" className="space-y-2">
            <p className="text-[13px] text-text">Dashboard execution is off, so agents cannot start yet.</p>
            <RunnerSwitch ws={ws} on testId="runner-enable" />
          </div>
        )}
        {needsAccess && !runnerOff && accessSelect}
        <RiskNotice startFull={startFull} loginFull={loginFull} loginNotice={loginNotice} onAcknowledge={() => void acknowledge(shownFull)} single={!!initial}>
          {data && <PreviewLines ws={ws} data={data} ticket={ticket} harness={shown?.args.harness ?? harness} />}
        </RiskNotice>
        <details className="group/more" data-testid="agent-more" open={moreOpen || (!!strong && checkFailing)} onToggle={(e) => setMoreOpen(e.currentTarget.open)}>
          <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-text-muted underline-offset-4 hover:underline [&::-webkit-details-marker]:hidden">More options</summary>
          <form className="mt-2 grid grid-cols-1 items-center gap-x-3 gap-y-2 sm:grid-cols-[110px_minmax(0,1fr)]" onSubmit={(e) => { e.preventDefault(); if (!badModel && !badLabel) void preview(form) }}>
            <Label htmlFor="agent-mode" className="text-xs text-text-muted">What it does</Label>
            <select id="agent-mode" className="h-9 min-w-0 rounded border border-border bg-bg px-2 sm:h-7" value={mode} onChange={(e) => setMode(e.target.value as AgentMode)}>
              {AGENT_MODES.map((m) => <option key={m} value={m}>{MODE_LABEL[m]}</option>)}
            </select>
            <Label htmlFor="agent-label" className="text-xs text-text-muted">Label</Label>
            <Input id="agent-label" className="h-9 min-w-0 text-xs sm:h-7" placeholder="optional, up to 40 characters" value={label} maxLength={40} autoComplete="off" onChange={(e) => setLabel(e.target.value)} />
            <span className="text-text-muted">Project folder</span>
            <label className="flex items-start gap-2 [&>input]:mt-1 [&>input]:shrink-0"><input type="checkbox" checked={worktree} onChange={(e) => setWorktree(e.target.checked)} /> Use this ticket's separate project folder when it has a branch</label>
            {models.length > 0 && <ModePickModels models={models} model={model} onModel={setModel} />}
            {strong && checkFailing && model !== strong && (<>
              <span className="hidden sm:block" />
              <p className="-mt-1 text-warning" data-testid="agent-strong-suggestion" role="note">
                A check on this ticket is failing. A stronger model may do better.{' '}
                <button type="button" className="underline" onClick={() => setModel(strong)}>Use {modelLabel(choices.data, harness, strong)}</button>
              </p>
            </>)}
            <span className="text-text-muted">Delegation</span>
            {harness === 'claude-code' ? (
              <div className="space-y-1" data-testid="agent-policy">
                <label className="flex items-start gap-2 [&>input]:mt-1 [&>input]:shrink-0"><input type="checkbox" checked={delegate} onChange={(e) => setDelegate(e.target.checked)} /> Let the main agent delegate</label>
                {delegate && <label className="flex items-center gap-2 text-xs text-text-muted">Up to
                  <select aria-label="Most delegated tasks" className="h-7 rounded border border-border bg-bg px-2" value={maxChildren} onChange={(e) => setMaxChildren(Number(e.target.value))}>{[1, 2, 3, 4, 5, 6, 7, 8, 9].map((n) => <option key={n} value={n}>{n}</option>)}</select> tasks</label>}
                <p className="text-xs text-text-muted">Applies at the next start. Off means the agent starts no helper agents.</p>
              </div>
            ) : <p className="text-xs text-text-muted" data-testid="agent-policy">Codex: delegation limits not available</p>}
            {harness === 'claude-code' && models.length > 0 && (<>
              <Label htmlFor="agent-subagent" className="text-xs text-text-muted">Model for helper agents</Label>
              <select id="agent-subagent" className="h-9 min-w-0 rounded border border-border bg-bg px-2 sm:h-7" value={subagent} onChange={(e) => setSubagent(e.target.value)}>
                <option value="">Chosen by the agent app</option>
                {models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
              </select>
            </>)}
          </form>
        </details>
        {loading && !data && <Skeleton className="h-6" />}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>Cancel</Button>
          <SignButton size="sm" label={primary.label} disabled={primary.disabled} after={busy || step === 'starting' ? 'Starting agent…' : null} slowLabel="Still starting…" onClick={primary.go} data-testid="agent-start" />
        </div>
      </div>
    </SignShell>
  )
}

const MODE_SENTENCE = (name: string): Record<AgentMode, string> => ({
  work: `${name} will work through this ticket and submit it for your review.`,
  refine: `${name} will help write the requirements and the plan. It changes no code.`,
  fix: `${name} will fix what a reviewer or a check sent back.`,
  continue: `${name} will pick up where an earlier agent stopped.`,
  context: `${name} will read the ticket and the code so you can ask questions. It changes nothing.`,
})

const CHANGED_NOTE = 'Something changed since you looked. Check and press Start again.'
/** The preview the person had on screen and the one after the grant say the same about what runs, where and with which model. */
const sameShown = (a: AgentPreview, b: AgentPreview) =>
  a.display === b.display && a.cwd_path === b.cwd_path && a.cwd.kind === b.cwd.kind && a.cwd.repo === b.cwd.repo && a.cwd.ticket === b.cwd.ticket &&
  (a.model ?? null) === (b.model ?? null) && (a.model_source ?? null) === (b.model_source ?? null) && a.will_create_worktree === b.will_create_worktree &&
  a.env.length === b.env.length && a.env.every((n, i) => n === b.env[i])
const unreadableOf = (d: AgentPreview) => d.display_hidden || d.cwd_path_hidden || d.display === null || d.cwd_path === null

/** The model rows of the options fold (the model is a name from core's list, never typed text). */
function ModePickModels({ models, model, onModel }: { models: { id: string; label: string }[]; model: string; onModel: (m: string) => void }) {
  return <ModelPick models={models} model={model} onModel={onModel} />
}

const isLoginNotice = (w: string) => w === LOGIN_NOTICE

/**
 * The "runs as you" disclosure (D-P2-31). First time: the full notice, with the login-shell notice, until the person
 * says they read it. After that: one small line, and the same words under Details. Never removed, only folded.
 */
function RiskNotice({ startFull, loginFull, loginNotice, onAcknowledge, single, children }: { /** Opened right after "Create and start agent": Start is the one button, and starting counts as reading the notice (as it always did). */ single?: boolean; startFull: boolean; loginFull: boolean; loginNotice: string[]; onAcknowledge: () => void; children: ReactNode }) {
  const full = startFull || loginFull
  const start = <p data-testid="execution-line">{EXECUTION_LINE}</p>
  const login = <>{loginNotice.map((x, i) => <p key={i} role="note" className="text-warning" data-testid="agent-login-notice"><Prose inline>{x}</Prose></p>)}</>
  return (
    <section aria-label="Runs as you" className="-mb-1 space-y-2 text-xs leading-5" data-testid="agent-risk">
      {full && (
        <div role="note" className="grid gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 font-normal text-warning sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start sm:[&>p]:col-start-1" data-testid="agent-risk-full">
          {startFull && start}
          {loginFull && login}
          {single
            ? <p className="font-normal" data-testid="agent-risk-single">Starting the agent confirms you have read this.</p>
            : <Button size="xs" variant="outline" className="justify-self-end font-normal sm:col-start-2 sm:row-start-1" onClick={onAcknowledge} data-testid="agent-risk-ack">I understand</Button>}
        </div>
      )}
      <details className="group/risk" data-testid="agent-details">
        <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 text-text-muted underline-offset-4 before:size-1.5 before:shrink-0 before:rotate-45 before:border-t before:border-r before:border-current hover:underline [&::-webkit-details-marker]:hidden group-open/risk:before:rotate-[135deg]">{startFull ? 'Details' : <>Runs as you on this computer <span aria-hidden>·</span> Details</>}</summary>
        <div className="mt-2 space-y-2">
          {(!startFull || !loginFull) && <div className="space-y-1 text-text-muted">{!startFull && start}{!loginFull && login}</div>}
          {children}
        </div>
      </details>
    </section>
  )
}

/** The host's preview, shown exactly: what runs, where (and whether a worktree is created first), env names, grant. */
function PreviewLines({ ws, data, ticket, harness }: { ws: string; data: AgentPreview; ticket?: string; harness: Harness }) {
  const where = data.cwd_path !== null ? data.cwd_path : null
  const choices = useQuery({ ...routingQueries.choices(ws), enabled: hasFeature('model_routing', ws) })
  const creates = data.will_create_worktree && data.cwd.kind === 'worktree' && where !== null
  return (
    <div className="space-y-2" data-testid="agent-preview">
      <dl className="grid grid-cols-1 gap-x-3 gap-y-0.5 sm:grid-cols-[110px_minmax(0,1fr)] sm:gap-y-1.5">
        <dt className="text-text-muted">Will use</dt>
        <dd data-testid="agent-model-line">{data.model ? modelLabel(choices.data, harness, data.model) : 'Chosen by the agent app'}{data.model_source === 'addon' ? ` (chosen by ${data.model_by ?? 'an add-on'})` : data.model_source === 'chosen' ? ' (your choice)' : ''}</dd>
        {data.grant && (<>
          <dt className="text-text-muted">Agent access</dt><dd data-testid="agent-grant">{data.grant.scope === 'all' ? 'All tickets' : 'Tickets you may work on'}, until {clock(data.grant.until)}. <details className="inline"><summary className="inline cursor-pointer text-text-muted">Technical details</summary> <Raw>{data.grant.id}</Raw></details></dd>
        </>)}
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
export function RunnerGrantCard({ ws, runner, loading, blocked, onChanged, initial, extend, onOutcome, onConfirmed, sheet }: { /** The short sheet layout (header pill): length chips, the exact end, what agents may do. */ sheet?: boolean; onConfirmed?: () => void; extend?: { id: string; scope: 'all' | 'workable'; until: string }; onOutcome?: () => void; initial?: GrantStep; ws: string; runner: Runner | null; loading: boolean; blocked: AgentPreview['blocked']; onChanged: () => void }) {
  const workspaces = useQuery(queries.workspaces())
  const workspace = workspaces.data?.find(w => w.id === ws)
  // 8 h / 1 / 3 / 7 days, never above the workspace maximum; an Extend never offers less than the access it replaces.
  // The maximum is the SIGNED setting (`settings.grant_hours`, from the host's settings read): the workspace list does not carry it.
  const hostSettings = useQuery({ ...queries.hostSettings(ws), retry: false })
  const maxHours = grantDefaultHours({ grant_hours: hostSettings.data?.grant_hours ?? workspace?.grant_hours })
  const maxKnown = !workspaces.isPending && !hostSettings.isPending // until both answered, the maximum is a guess: show nothing about it
  const riskText = maxKnown ? DEPOSIT_RISK.replace('up to 7 days', `up to ${hoursText(maxHours)}`) : DEPOSIT_RISK.replace(' (up to 7 days, the length you choose)', ' (the length you choose)')
  // Shown in full until acknowledged once (kept per person and workspace); an unreadable answer counts as not acknowledged.
  const qcRisk = useQueryClient()
  const ackQ = useQuery({ queryKey: ['risk-notices', ws], queryFn: () => agentsApi.riskNotices(ws), retry: false, enabled: !!sheet })
  const grantFull = ackQ.data?.acknowledged.grant !== true
  const ackGrant = async () => { try { qcRisk.setQueryData(['risk-notices', ws], await agentsApi.acknowledge(ws, 'grant')) } catch { /* shown in full again next time */ } }
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
  const oneClick = clickReady(workspace)
  const [handoff, setHandoff] = useState(false)
  const changed = useRef(onChanged)
  changed.current = onChanged
  const outcome = useRef(onOutcome)
  outcome.current = onOutcome
  const confirmed = useRef(onConfirmed)
  confirmed.current = onConfirmed
  useEffect(() => {
    if (['polling', 'confirmed', 'rejected', 'unknown'].includes(step.kind)) outcome.current?.()
    // A signed and delivered grant ends the dialog that hosts this card (Extend), so a second click cannot extend again.
    if (step.kind === 'confirmed') confirmed.current?.()
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
      {!sheet && <p className="flex items-center gap-2 font-medium text-text"><KeyRound className="size-3.5 text-text-muted" aria-hidden />Allow agents to work for you</p>}
      {sheet ? (
        <div data-testid="runner-grant-risk" className="space-y-1">
          {grantFull ? (
            <div role="note" className="space-y-2 rounded-md border border-border bg-surface px-3 py-2"><p>{riskText}</p><Button size="xs" variant="outline" data-testid="grant-risk-ack" onClick={() => void ackGrant()}>I understand</Button></div>
          ) : (
            <details data-testid="grant-risk-details"><summary className="cursor-pointer text-text-muted">Runs as you on this computer <span aria-hidden>·</span> Details</summary><p className="mt-1 text-text-muted">{riskText}</p></details>
          )}
        </div>
      ) : <p role="note" data-testid="runner-grant-risk" className="text-warning">{riskText}</p>}
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
      {extend && !sheet && <p>Agents that are running now stop; new agents use the extended access.</p>}
      {/* M-2: while the terminal is waiting for the person (the signature, then r), the old "issue one" must not shout over it. */}
      {blocked && (handoff || step.kind === 'polling'
        ? <p className="text-text" data-testid="runner-grant-waiting">Review and sign the agent access.</p>
        : <p className="text-text" data-testid="runner-grant-needed">{blockedText(blocked)}</p>)}
      {(!grant || extend) && !loading && maxKnown && step.kind !== 'confirmed' && (click.phase === 'idle' || click.phase === 'failed' || click.phase === 'cancelled') && (
        lengths.length === 0 ? <p className="text-text-muted" data-testid="runner-grant-longest">Your access already lasts as long as this workspace allows ({hoursText(maxHours)}), so there is nothing to extend. Whoever owns the workspace can allow longer in Settings, General.</p> : sheet ? <div className="min-w-0 space-y-4 py-2 text-[13px] leading-relaxed" data-testid="access-sheet">
          <div role="radiogroup" aria-label="Allow access for" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {lengths.map(([h, label]) => <button key={h} type="button" role="radio" aria-checked={h === hours} onClick={() => setHours(h)} className={`min-h-11 min-w-0 rounded-lg border px-2 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${h === hours ? 'border-warning bg-warning/10 text-warning' : 'border-border bg-surface text-text hover:bg-bg'}`}>{label}</button>)}
          </div>
          <p data-testid="access-until" className="break-words text-xs leading-relaxed text-text-muted">Until <time dateTime={new Date(nowMs() + hours * 3_600_000).toISOString()}>{fmtExact(new Date(nowMs() + hours * 3_600_000).toISOString())}</time> (your time), counted from now.{extend && <> Replaces your current access, which ends <time dateTime={extend.until}>{fmtExact(extend.until)}</time>.</>}</p>
          <p data-testid="access-may-do" className="border-t border-border pt-4 text-text-muted"><span className="font-medium text-text">What agents may do. </span>Agents (Claude Code) may read and change files in the project folder and run its commands, on {scope === 'all' ? 'all tickets' : 'the tickets you may work on'}.</p>
          <p className="text-xs text-text-muted">This workspace allows access for up to {hoursText(maxHours)}.{extend ? ' Agents that are running now stop; new agents use the extended access.' : ''}</p>
          <Button disabled={!enabled || false} className="min-h-11 w-full" onClick={() => void issue()} data-testid="runner-grant-issue">{extend ? 'Extend access' : 'Allow agent access'}</Button>
          {!runner?.enabled && runner && <><span className="text-text-muted" data-testid="runner-grant-off">{TERMINAL_ONLY}</span><RunnerSwitch ws={ws} on testId="runner-grant-enable" variant="secondary" /></>}
        </div> : <div className="flex flex-wrap items-center gap-2">
          <Label htmlFor="grant-hours" className="text-xs text-text-muted">Length</Label>
          <select id="grant-hours" className="h-9 rounded border border-border bg-bg px-2 sm:h-7" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {lengths.map(([h, label]) => <option key={h} value={h}>{label}</option>)}
          </select>
          <Label htmlFor="grant-scope" className="text-xs text-text-muted">Tickets</Label>
          <select id="grant-scope" disabled={!!extend} className="h-9 rounded border border-border bg-bg px-2 sm:h-7" value={scope} onChange={(e) => setScope(e.target.value as 'workable' | 'all')}>
            <option value="workable">Tickets you may work on</option>
            <option value="all">All tickets</option>
          </select>
          <Button size="xs" disabled={!enabled || false} onClick={() => void issue()} data-testid="runner-grant-issue">{extend ? 'Extend access' : 'Issue runner grant'}</Button>
          {!runner?.enabled && runner && <><span className="text-text-muted" data-testid="runner-grant-off">{TERMINAL_ONLY}</span><RunnerSwitch ws={ws} on testId="runner-grant-enable" variant="secondary" /></>}
        </div>
      )}
      {handoff && workspace && action.hours > 0 && <SignPopover ws={ws} title={extend ? 'Extend agent access' : 'Issue runner grant'} action={action}
        summary={extend ? <div className="space-y-1" data-testid="extend-summary">
          <p>Your current agent access ends {fmtDateTime(extend.until)} <span className="text-text-muted">(per host, not signed)</span>. The new access lasts {hoursText(hours)} from the moment you sign.</p>
        </div> : <p>Agents on this machine can act for you on {scope === 'all' ? 'all tickets' : 'the tickets you may work on'} for {hoursText(hours)}. One click must be ready before you can allow agents.</p>}
        onSettled={(phase, _state, request) => {
          if (phase !== 'done') return
          setHandoff(false)
          setStep({ kind: 'polling', depositId: depositIdOf(request?.id), grant: null })
        }} onClose={() => setHandoff(false)} />}
      {(click.phase === 'preparing' || click.phase === 'viewing') && <Skeleton className="h-16" />}
      {view && (click.phase === 'ready' || click.phase === 'signing') && (viewOk ? (
        <ClickSignPanel ws={ws} story={storyOf(action)} answer={view} problem={click.problem} signing={click.phase === 'signing'} onSign={() => void click.sign()} onCancel={() => void click.cancel().then(() => click.reset(true))} />
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
    return <ClickSignPanel answer={click.view} problem={click.problem} signing={click.phase === 'signing'} onSign={() => void click.sign()} onCancel={() => void click.cancel().then(() => click.reset(true))} />
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
export const FIND_GRANT = 'Its id did not reach this page: revoke your newest runner grant under Settings → Grants.'

/** The grant's refusals before signing, in the approved words. */
export function refusalText(e: unknown): string | null {
  if (!(e instanceof ApiError)) return null
  if (e.code === 'human.click_refused' && e.reason === 'terminal_only') return TERMINAL_ONLY
  if (e.code === 'human.click_refused' && e.reason === 'disabled') return TERMINAL_GRANT // review I4: click is off here
  if (e.code === 'click.unavailable' || e.code === 'click.contract' || e.code === 'sign.convenience_unavailable') return TERMINAL_GRANT
  return null
}
