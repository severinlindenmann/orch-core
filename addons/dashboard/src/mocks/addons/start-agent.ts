import type { LaunchHarness, LaunchMode, LaunchPreview, LaunchWhere } from '@/api/types'
import { HARNESSES, HARNESS_LABEL, MODES, MODE_LABEL, WHERES, WHERE_LABEL, type LaunchRequest } from '../sessions'
import { canSeeTicket, registerAddon, type AddonCtx } from './registry'

// start-agent (capability spawn_agent): pick a mode, a harness and where it runs, see the exact command, press Start.
//  - The addon never starts anything itself. `start` is declared `confirm: 'spawn_agent'` in the manifest: core shows
//    its own dialog (and signs a grant first when the person has none), then posts `start` with core's `confirmed`
//    flag. The action asks core (`store.startSession`), which checks the person, the ticket and the grant, registers
//    the session on that grant and plays the simulated run.
//  - The choice is the viewer's own, per ticket (`state.nav[viewer]`). The command comes from core's resolver
//    (`store.resolveLaunch`), the same one the start uses, so the preview is the command that runs.
//  - Everything per ticket (previews, runs, the panel) is built only for tickets the viewer can see in this workspace.

interface Choice {
  mode: LaunchMode
  harness: LaunchHarness
  where: LaunchWhere
}
interface Nav {
  choices: Record<string, Choice>
  /** The ticket picked on the Start agent page. */
  selected?: string
}
type Ctx = Pick<AddonCtx, 'store' | 'ws' | 'viewer'>

const DEFAULTS = { harness: 'claude-code' as LaunchHarness, where: 'background' as LaunchWhere }
const one = <T extends string>(list: readonly T[], v: unknown, fallback: T): T => (list.includes(v as T) ? (v as T) : fallback)
const settingsOf = (state: Record<string, unknown>) => {
  const s = (state.settings ?? {}) as Partial<typeof DEFAULTS>
  return { harness: one(HARNESSES, s.harness, DEFAULTS.harness), where: one(WHERES, s.where, DEFAULTS.where) }
}
const navOf = (state: Record<string, unknown>, viewer: string): Nav => {
  const n = ((state.nav ?? {}) as Record<string, Partial<Nav>>)[viewer] ?? {}
  return { choices: { ...(n.choices ?? {}) }, selected: n.selected }
}
const setNav = (state: Record<string, unknown>, viewer: string, nav: Nav) => {
  ;((state.nav ??= {}) as Record<string, Nav>)[viewer] = nav
}
const choiceOf = (state: Record<string, unknown>, viewer: string, key: string): Choice => {
  const d = settingsOf(state)
  return navOf(state, viewer).choices[key] ?? { mode: 'work', harness: d.harness, where: d.where }
}
const parseChoice = (raw: unknown, fallback: Choice): Choice => {
  const f = (raw ?? {}) as Record<string, unknown>
  return { mode: one(MODES, f.mode, fallback.mode), harness: one(HARNESSES, f.harness, fallback.harness), where: one(WHERES, f.where, fallback.where) }
}
const nameOf = (c: Ctx, person: string) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === person)?.name ?? person
const hhmm = (iso: string) => `${iso.slice(11, 16)} UTC`

/** Tickets the viewer can see in this workspace that are not done (where a run makes sense). */
const startable = (c: Ctx) =>
  c.store
    .ticketKeys(c.ws)
    .filter((k) => canSeeTicket(c, k))
    .map((k) => c.store.ticket(k)!)
    .filter((t) => t.status !== 'done')
    .sort((a, b) => a.key.localeCompare(b.key))

function preview(c: Ctx, state: Record<string, unknown>, key: string, title: string): LaunchPreview {
  const choice = choiceOf(state, c.viewer, key)
  const req: LaunchRequest = { ticket: key, ...choice }
  const { plan, command } = c.store.resolveLaunch(c.ws, req)
  return {
    ticket: key,
    title,
    mode: MODE_LABEL[choice.mode],
    harness: HARNESS_LABEL[choice.harness],
    where: WHERE_LABEL[choice.where],
    command,
    ...(plan.line ? { model: plan.line } : {}),
    ...(plan.error ? { blocked: plan.error } : {}),
  }
}

/** Sessions started from the dashboard on tickets the viewer can see, with their live state. */
function runsOf(c: Ctx) {
  const agents = c.store.agents(c.ws)
  return c.store
    .startedSessions(c.ws)
    .filter((s) => canSeeTicket(c, s.ticket))
    .map((s) => {
      const a = agents.find((x) => x.session === s.session)
      return { s, state: a?.state ?? 'stopped', step: c.store.ticket(s.ticket)?.tasks_state.find((t) => t.state === 'doing') }
    })
}

const EMPTY = { type: 'stack', children: [] }
const SCHEMA_PROPS = {
  mode: { type: 'string', title: 'Mode', oneOf: MODES.map((m) => ({ const: m, title: MODE_LABEL[m] })) },
  harness: { type: 'string', title: 'Harness', oneOf: HARNESSES.map((h) => ({ const: h, title: HARNESS_LABEL[h] })) },
  where: { type: 'string', title: 'Where', oneOf: WHERES.map((w) => ({ const: w, title: WHERE_LABEL[w] })) },
}

/** The preview block: model line, the exact command, a blocking sentence, and Start (core confirms it). */
function previewNode(p: LaunchPreview) {
  return {
    type: 'stack',
    children: [
      ...(p.model ? [{ type: 'markdown', text: p.model }] : []),
      { type: 'code', language: 'bash', text: p.command },
      p.blocked ? { type: 'alert', tone: 'warn', title: 'Start is blocked', text: p.blocked } : EMPTY,
      { type: 'button', label: 'Start', action: 'start', variant: 'primary' },
    ],
  }
}

registerAddon({
  name: 'start-agent',
  seed: () => ({ settings: { ...DEFAULTS }, nav: {} }),

  view(state, c) {
    const tickets = startable(c)
    const runs = runsOf(c)
    const live = runs.filter((r) => r.state !== 'stopped')
    const previews: Record<string, LaunchPreview> = {}
    const byTicket: Record<string, unknown> = {}
    for (const t of tickets) {
      const p = (previews[t.key] = preview(c, state, t.key, t.title))
      const run = live.find((r) => r.s.ticket === t.key)
      byTicket[t.key] = {
        running: !!run,
        ...(run ? { session: run.s.session } : {}),
        run: run
          ? {
              type: 'stack',
              children: [
                {
                  type: 'kv',
                  pairs: [
                    { label: 'Session', value: `${run.s.name} · ${run.s.session}` },
                    { label: 'State', value: run.state === 'waiting' ? `waiting on ${nameOf(c, run.s.for)}` : run.state },
                    { label: 'Doing', value: run.step ? `${run.step.id} ${run.step.text}` : null },
                    { label: 'Mode', value: MODE_LABEL[run.s.mode] },
                    ...(run.s.model ? [{ label: 'Model', value: run.s.model, mono: true }] : []),
                    { label: 'Started', value: `${hhmm(run.s.started_at)} by ${nameOf(c, run.s.for)}` },
                  ],
                },
                { type: 'button', label: 'Stop', action: 'stop', variant: 'danger' },
              ],
            }
          : EMPTY,
        setup: run
          ? EMPTY
          : {
              type: 'stack',
              children: [
                { type: 'form', schema: { type: 'object', properties: SCHEMA_PROPS }, formData: choiceOf(state, c.viewer, t.key), action: 'configure', submitLabel: 'Update command' },
                previewNode(p),
              ],
            },
      }
    }
    const nav = navOf(state, c.viewer)
    const selected = nav.selected && previews[nav.selected] ? nav.selected : null
    return {
      settings: settingsOf(state),
      previews,
      byTicket,
      selected,
      ticketOptions: tickets.length ? tickets.map((t) => ({ const: t.key, title: `${t.key} · ${t.title}` })) : [{ const: '', title: 'No open tickets' }],
      page: {
        form: { ...(selected ? { ticket: selected, ...choiceOf(state, c.viewer, selected) } : {}) },
        preview: selected ? previewNode(previews[selected]) : { type: 'markdown', text: 'Pick a ticket and press **Update command** to see what will run.' },
      },
      runs: live.map((r) => ({
        session: r.s.session,
        ticket: r.s.ticket,
        title: c.store.ticket(r.s.ticket)!.title,
        mode: MODE_LABEL[r.s.mode],
        harness: r.s.name,
        model: r.s.model ?? 'default',
        state: r.state === 'waiting' ? `waiting on ${nameOf(c, r.s.for)}` : r.state,
        started: hhmm(r.s.started_at),
      })),
      ended: runs
        .filter((r) => r.state === 'stopped')
        .reverse()
        .slice(0, 10)
        .map((r) => ({ title: `${r.s.ticket} · ${r.s.name} · ${r.s.session}`, subtitle: `${MODE_LABEL[r.s.mode]}, started ${hhmm(r.s.started_at)}${r.s.stopped ? `, ${r.s.stopped.reason} ${hhmm(r.s.stopped.at)}` : ''}`, status: 'idle' })),
    }
  },

  actions: {
    configure(ctx) {
      const { state, viewer, body } = ctx
      const form = (body.formData ?? {}) as Record<string, unknown>
      const nav = navOf(state, viewer)
      let key = ctx.ticket
      if (!key) {
        if (typeof form.ticket !== 'string' || !canSeeTicket(ctx, form.ticket)) return { ok: false, status: 400, code: 'validation', message: 'Pick a ticket.' }
        key = form.ticket
        nav.selected = key
      }
      nav.choices[key] = parseChoice(form, choiceOf(state, viewer, key))
      setNav(state, viewer, nav)
      return { ok: true, message: `Command updated for ${key}.`, changed: true }
    },
    start(ctx) {
      const { state, viewer, store, ws } = ctx
      const key = ctx.ticket ?? navOf(state, viewer).selected
      if (!key) return { ok: false, status: 400, code: 'validation', message: 'Pick a ticket first.' }
      if (!canSeeTicket(ctx, key)) return { ok: false, status: 404, code: 'not_found', message: `No ticket ${key}` }
      const res = store.startSession(ws, { addon: 'start-agent', ticket: key, ...choiceOf(state, viewer, key) }, { kind: 'person', id: viewer })
      if (!res.ok) return res
      return { ok: true, message: `Started ${res.session.name} on ${key} (${res.session.session}).`, changed: true }
    },
    stop(ctx) {
      const { store, ws, viewer, body } = ctx
      const live = runsOf(ctx).filter((r) => r.state !== 'stopped')
      const run = typeof body.session === 'string' ? live.find((r) => r.s.session === body.session) : live.find((r) => r.s.ticket === ctx.ticket)
      if (!run) return { ok: true, message: ctx.ticket ? `No run on ${ctx.ticket}.` : 'That run has already ended.' }
      const res = store.stopSession(ws, run.s.session, { kind: 'person', id: viewer })
      if (!res.ok) return res
      return { ok: true, message: `Stopped ${run.s.session} on ${run.s.ticket}.`, changed: true }
    },
    save_settings: ({ state, body }) => {
      state.settings = settingsOf({ settings: body.formData })
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
