// Agent sessions started from the dashboard (core, not an addon): the launch resolver that builds the exact command,
// the fold of agent.started / agent.stopped workspace events, and the simulated run core plays on the ticket.
import type { Actor, LaunchHarness, LaunchMode, LaunchWhere, TicketDocument, WorkspaceEvent } from '@/api/types'
import type { SimStep } from './sim'
import type { MockStore } from './store'

export const MODE_LABEL: Record<LaunchMode, string> = { refine: 'Refine', work: 'Work on ticket', fix: 'Fix failing checks', continue: 'Continue after feedback' }
export const HARNESS_LABEL: Record<LaunchHarness, string> = { 'claude-code': 'Claude Code', codex: 'Codex' }
export const WHERE_LABEL: Record<LaunchWhere, string> = { terminals: 'Terminals', background: 'Background' }
export const MODES = Object.keys(MODE_LABEL) as LaunchMode[]
export const HARNESSES = Object.keys(HARNESS_LABEL) as LaunchHarness[]
export const WHERES = Object.keys(WHERE_LABEL) as LaunchWhere[]

export interface LaunchRequest {
  ticket: string
  mode: LaunchMode
  harness: LaunchHarness
  where: LaunchWhere
}

/** What a `launch` addon (model routing) adds to a start. Empty: the harness default. */
export interface LaunchPlan {
  model?: string
  tier?: string
  subagentModel?: string
  /** One line for the preview: "Model · work runs on standard: Standard (sonnet); subagents on haiku". */
  line?: string
  /** A sentence that blocks the start (a setting that is not a model name). */
  error?: string
  /** The addon that made this plan (set by core). */
  by?: string
}

/** The exact command core runs for a request and plan. Model names are validated before they get here. */
export function commandFor(req: LaunchRequest, plan: LaunchPlan): string {
  const prompt = `"/orch:${req.mode} ${req.ticket}"`
  const claude = req.harness === 'claude-code'
  const tool = claude ? `claude${plan.model ? ` --model ${plan.model}` : ''} ${prompt}` : `codex ${prompt}`
  const env = claude && plan.subagentModel ? `CLAUDE_CODE_SUBAGENT_MODEL=${plan.subagentModel} ` : ''
  return `${env}orch session start --in ${req.where} ${req.ticket} -- ${tool}`
}

export interface StartedSession {
  session: string
  agent: LaunchHarness
  name: string
  for: string
  grant: string
  ticket: string
  mode: LaunchMode
  where: LaunchWhere
  model?: string
  tier?: string
  command: string
  started_at: string
  stopped: { at: string; reason: string } | null
}

/** Sessions started from the dashboard, from the workspace log (oldest first). */
export function foldSessions(events: WorkspaceEvent[]): StartedSession[] {
  const out: StartedSession[] = []
  for (const e of events) {
    if (e.type === 'agent.started') {
      const agent = e.agent === 'codex' ? 'codex' : 'claude-code'
      out.push({
        session: String(e.session),
        agent,
        name: HARNESS_LABEL[agent],
        for: String(e.for),
        grant: String(e.grant),
        ticket: String(e.ticket),
        mode: (MODES as string[]).includes(String(e.mode)) ? (e.mode as LaunchMode) : 'work',
        where: e.where === 'terminals' ? 'terminals' : 'background',
        model: typeof e.model === 'string' ? e.model : undefined,
        tier: typeof e.tier === 'string' ? e.tier : undefined,
        command: String(e.command ?? ''),
        started_at: e.at,
        stopped: null,
      })
    } else if (e.type === 'agent.stopped') {
      const s = out.find((x) => x.session === e.session)
      if (s && !s.stopped) s.stopped = { at: e.at, reason: String(e.reason ?? 'stopped') }
    }
  }
  return out
}

export const agentActor = (s: StartedSession): Actor => ({ kind: 'agent', id: s.agent, session: s.session, for: s.for, grant: s.grant })

const VERB: Record<LaunchMode, string> = { refine: 'Refining', work: 'Working on', fix: 'Fixing the checks of', continue: 'Picking up feedback on' }

/** The next task to work on: one in progress, else the first one to do. */
const nextTask = (doc: TicketDocument | undefined) => doc?.tasks_state.find((t) => t.state === 'doing') ?? doc?.tasks_state.find((t) => t.state === 'todo')

/**
 * The simulated run: claim → start the next task (lease) → a log line → the task done with a receipt → a blocking
 * question to the person the session works for → wait. Each step reads the ticket as it is then; the script is
 * played under the session id, so stopping the session (or revoking its grant) ends it.
 */
export function sessionScript(s: StartedSession, expires: string): SimStep[] {
  const actor = agentActor(s)
  const key = s.ticket
  let task: string | undefined
  const steps: SimStep[] = [
    {
      afterMs: 1500,
      run: (st: MockStore) => {
        const doc = st.ticket(key)
        if (!doc || doc.claim) return
        st.append(key, { type: 'claim.taken', actor, expires })
        if (doc.status === 'open' || doc.status === 'backlog') st.append(key, { type: 'status.changed', actor: 'host', to: 'in-progress' })
      },
    },
    {
      afterMs: 2000,
      run: (st) => {
        const t = nextTask(st.ticket(key))
        task = t?.id
        if (t && t.state === 'todo') st.append(key, { type: 'lease.taken', actor, task: t.id })
      },
    },
    {
      afterMs: 3000,
      run: (st) => {
        const t = st.ticket(key)?.tasks_state.find((x) => x.id === task)
        st.append(key, { type: 'log.added', actor, text: t ? `${VERB[s.mode]} ${t.id}: ${t.text}` : `${VERB[s.mode]} ${key}: reading the ticket` })
      },
    },
    {
      afterMs: 4000,
      run: (st) => {
        if (task) st.append(key, { type: 'task.done', actor, task, receipt: { exit: 0, ms: 38_400 } })
      },
    },
    {
      afterMs: 2000,
      run: (st) => {
        const doc = st.ticket(key)
        if (!doc) return
        const next = nextTask(doc)
        const id = `Q${doc.questions_state.length + 1}`
        const text = task
          ? next
            ? `${task} is done. Go on with ${next.id} (${next.text})?`
            : `${task} is done. Anything to change before I write the handoff?`
          : 'I read the ticket. Go on, or do you want to change something first?'
        const def = { id, to: s.for, text, options: [{ key: 'go', label: 'Go on' }, { key: 'wait', label: 'Wait for me' }], recommended: 'go', blocking: true }
        st.append(key, { type: 'question.asked', actor, question: id, def })
      },
    },
  ]
  return steps
}
