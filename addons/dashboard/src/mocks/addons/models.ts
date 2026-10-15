import { isModelName } from '@/api/launch'
import type { AddonDecision } from '@/api/types'
import { canSeeTicket, conflict, markDecided, registerAddon, type AddonCtx } from './registry'
import type { LaunchPlan } from '../sessions'

// models (model routing, capability `launch`; v1 addons/model-routing): which model a start-agent session starts on.
//  - Settings (owner): Light/Standard/Strong model names, a tier per mode, the subagent model. The mockup seeds the
//    recommended aliases so enabling it shows the effect at once (v1 starts empty).
//  - `launch()` is called by core (store.resolveLaunch) for the preview and the start. Claude Code only; Codex starts on
//    its own default. A setting that is not a model name (spaces, a leading "-") blocks every start with one sentence.
//  - Escalation (a human's choice): a task whose check failed in two or more agent sessions and is still open gets a
//    core-rendered decision on Today. "Next start on Strong" makes the next start on that ticket run on Strong, once.
//    "Not now" hides it until another session fails the task (the decision id carries the session count).

type Tier = 'light' | 'standard' | 'strong'
interface Settings {
  light: string
  standard: string
  strong: string
  refine: string
  work: string
  fix: string
  continue: string
  subagent: string
}
const TIERS: Tier[] = ['light', 'standard', 'strong']
const TIER_LABEL: Record<Tier, string> = { light: 'Light', standard: 'Standard', strong: 'Strong' }
const MODE_TIERS = ['light', 'standard', 'strong', 'none']
const DEFAULTS: Settings = { light: 'haiku', standard: 'sonnet', strong: 'opus', refine: 'strong', work: 'standard', fix: 'standard', continue: 'same', subagent: 'haiku' }
const MODEL_FIELDS: { key: 'light' | 'standard' | 'strong' | 'subagent'; label: string }[] = [
  { key: 'light', label: 'Light' },
  { key: 'standard', label: 'Standard' },
  { key: 'strong', label: 'Strong' },
  { key: 'subagent', label: 'subagent' },
]
const str = (v: unknown, max = 80) => (typeof v === 'string' ? v.trim().slice(0, max) : '')

const settingsOf = (state: Record<string, unknown>): Settings => {
  const s = (state.settings ?? {}) as Partial<Record<keyof Settings, unknown>>
  const tier = (v: unknown, fallback: string, extra: string[] = []) => ([...MODE_TIERS, ...extra].includes(v as string) ? (v as string) : fallback)
  return {
    light: str(s.light),
    standard: str(s.standard),
    strong: str(s.strong),
    subagent: str(s.subagent),
    refine: tier(s.refine, DEFAULTS.refine),
    work: tier(s.work, DEFAULTS.work),
    fix: tier(s.fix, DEFAULTS.fix),
    continue: tier(s.continue, DEFAULTS.continue, ['same']),
  }
}
const invalidOf = (s: Settings) => MODEL_FIELDS.filter((f) => s[f.key] !== '' && !isModelName(s[f.key]))
const nextStrong = (state: Record<string, unknown>) => (state.strongNext ??= {}) as Record<string, boolean>

type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>
const COUNT_WORD = ['zero', 'one', 'two']

/** Tasks still open whose check failed in two or more distinct agent sessions, with the latest failing log lines. */
function failingTasks(c: Ctx) {
  const out: { ticket: string; task: string; sessions: number; detail?: string }[] = []
  for (const key of c.store.ticketKeys(c.ws)) {
    const doc = c.store.ticket(key)
    if (!doc) continue
    for (const t of doc.tasks_state) {
      if (t.state === 'done' || t.state === 'skipped') continue
      const runs = c.store.eventsOf(key).filter((e) => e.type === 'task.run' && e.task === t.id && e.actor.kind === 'agent' && (e.receipt as { exit?: number } | undefined)?.exit !== 0)
      const sessions = new Set(runs.map((e) => (e.actor as { session: string }).session.split('.')[0]))
      if (sessions.size < 2) continue
      const log = [...doc.artifacts].reverse().find((a) => a.task === t.id && a.kind === 'log' && a.preview)
      const lines = log?.preview?.trimEnd().split('\n').slice(-3).join('\n')
      out.push({ ticket: key, task: t.id, sessions: sessions.size, detail: lines ? `Last lines of ${log!.name}:\n${lines}` : undefined })
    }
  }
  return out
}

const decisionId = (f: { ticket: string; task: string; sessions: number }) => `models.escalate:${f.ticket}:${f.task}:${f.sessions}`

registerAddon({
  name: 'models',
  seed: () => ({ settings: { ...DEFAULTS }, strongNext: {}, decided: [] }),

  view(state, c) {
    const s = settingsOf(state)
    const bad = invalidOf(s)
    return {
      settings: s,
      settingsAlert: bad.length
        ? { type: 'alert', tone: 'warn', title: `${bad.map((f) => (f.key === 'subagent' ? 'Subagent model' : f.label)).join(', ')} ${bad.length === 1 ? 'is' : 'are'} not a model name`, text: 'Use a model name or alias without spaces that does not start with "-". Start agent is blocked until then.' }
        : { type: 'stack', children: [] },
      strongNext: undefined,
      nextStrong: Object.keys(nextStrong(state)).filter((k) => canSeeTicket(c, k)),
      decided: undefined,
    }
  },

  decisions(state, _pkg, c): AddonDecision[] {
    const done = (state.decided as string[] | undefined) ?? []
    const strong = nextStrong(state)
    return failingTasks(c)
      .filter((f) => !done.includes(decisionId(f)) && !strong[f.ticket])
      .map((f) => ({
        kind: 'decision',
        id: decisionId(f),
        addon: 'models',
        ticket: f.ticket,
        title: 'Model routing',
        question: `${f.task} failed its check in ${f.sessions <= 2 ? COUNT_WORD[f.sessions] : f.sessions} sessions: start the next session on Strong?`,
        ...(f.detail ? { detail: f.detail } : {}),
        options: [
          { key: 'strong', label: 'Next start on Strong', primary: true },
          { key: 'not_now', label: 'Not now' },
        ],
        action: 'escalate',
      }))
  },

  launch(state, req, c): LaunchPlan {
    const s = settingsOf(state)
    const bad = invalidOf(s)[0]
    if (bad) return { error: `The ${bad.label} model "${s[bad.key]}" is not a model name (no spaces, no leading "-"). Start is blocked until it is fixed in the Model routing settings.` }
    if (req.harness !== 'claude-code') return { line: "Model: Codex's own default (model routing covers Claude Code)" }
    const strong = nextStrong(state)
    const escalated = !!strong[req.ticket]
    if (escalated && c.commit) delete strong[req.ticket]
    const configured = s[req.mode]
    const tier = (escalated ? 'strong' : configured === 'same' ? (TIERS.includes(c.lastTier as Tier) ? c.lastTier : 'standard') : configured) as Tier | 'none'
    const sub = s.subagent || undefined
    const subs = sub ? ` · subagents on ${sub}` : ''
    // Light without a Light model uses Standard; a tier without a model starts on the harness default.
    const modelTier: Tier | undefined = tier === 'none' ? undefined : tier === 'light' && !s.light ? 'standard' : tier
    const model = modelTier ? s[modelTier] : ''
    // One plain line: the tier by its name and the model it maps to ("Model: Standard (claude-sonnet-5-5)").
    if (tier === 'none' || !model) return { subagentModel: sub, line: `Model: the harness default${subs}` }
    const why = escalated ? ', after failed checks' : tier === 'light' && modelTier !== 'light' ? ', as Light has no model' : ''
    return { model, tier, subagentModel: sub, line: `Model: ${TIER_LABEL[modelTier!]} (${model})${why}${subs}` }
  },

  actions: {
    escalate(ctx) {
      // A decision action: core checked who decides, that it is open and that the option is one of its options.
      const { state, body } = ctx
      const open = ctx.decision
      if (!open) return conflict('decision.closed', 'That decision is closed.') // only when the manifest lacks `decision: true`
      markDecided(state, open.id)
      if (body.option === 'strong') {
        nextStrong(state)[open.ticket!] = true
        return { ok: true, message: `The next start on ${open.ticket} runs on Strong.`, changed: true }
      }
      return { ok: true, message: 'Not now. It comes back if another session fails the check.', changed: true }
    },
    save_settings: ({ state, body }) => {
      state.settings = settingsOf({ settings: body.formData })
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
