import type { Rng } from '../busy/rng'
import type { MockStore } from '../store'
import { canSeeTicket, conflict, registerAddon, type AddonCtx } from './registry'
import { fmtExact, fmtWhen } from '@/lib/time'

// records: commits and pushes orch's ticket records to git (v1 C29).
//  - Pending changes are derived, never stored: per ticket, the events whose seq is above the seq recorded by the last
//    record commit (`state.recorded[key]`), counted from store.eventsOf. A commit moves every ticket's mark to its
//    latest seq, so the list empties and new ticket activity makes it pending again.
//  - The viewer's pending list, counts and summary cover only tickets the viewer can see. A commit is workspace-wide
//    (it records every ticket); its history entry carries aggregate counts and no ticket key.
//  - The remote is a mock: a push succeeds and the "remote" then moves on (someone else pushed), so a second push without
//    a pull is rejected as non-fast-forward. Pull catches up.

interface Commit {
  hash: string
  at: string
  by: string
  /** Events recorded per ticket. Shown to a person only for the tickets they can see. */
  perTicket: Record<string, number>
}
interface Settings {
  auto_commit_minutes: number
  push: boolean
  remote: string
}
interface LastPush {
  at: string
  commit: string
  remote: string
}
const ADDON = { kind: 'addon', id: 'records' } as const
const REMOTE = 'git@github.com:acme-energy/energy-records.git'
const DEFAULTS: Settings = { auto_commit_minutes: 30, push: true, remote: REMOTE }
const REJECTED = 'Remote rejected: non-fast-forward. Pull first.'
const SEED_PENDING = [3, 4, 2, 3] // events not recorded yet, for the first tickets that have that many

const hashOf = (n: number) => (Math.imul(n + 7, 2654435761) >>> 0).toString(16).padStart(8, '0').slice(0, 7)
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
const nameOf = (c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>) => c.store.workspaces.find((w) => w.id === c.ws)?.members.find((m) => m.person === c.viewer)?.name ?? c.viewer
const lastSeq = (store: AddonCtx['store'], key: string) => store.eventsOf(key).at(-1)?.seq ?? 0

const settingsOf = (state: Record<string, unknown>): Settings => {
  const s = (state.settings ?? {}) as Partial<Settings>
  return {
    auto_commit_minutes: Number.isInteger(s.auto_commit_minutes) && s.auto_commit_minutes! > 0 && s.auto_commit_minutes! <= 1440 ? s.auto_commit_minutes! : DEFAULTS.auto_commit_minutes,
    push: s.push === true,
    remote: typeof s.remote === 'string' && s.remote.trim() && s.remote.length <= 200 && !/[\r\n]/.test(s.remote) ? s.remote.trim() : DEFAULTS.remote,
  }
}
/** A seed commit spread over `n` tickets (from `from`) with `events` events in all. */
const spread = (keys: string[], from: number, n: number, events: number): Record<string, number> => {
  const out: Record<string, number> = {}
  const picked = keys.slice(from, from + n)
  picked.forEach((k, i) => (out[k] = Math.floor(events / picked.length) + (i < events % picked.length ? 1 : 0)))
  return out
}
const history = (state: Record<string, unknown>) => state.history as Commit[] // newest first
const recorded = (state: Record<string, unknown>) => state.recorded as Record<string, number>

/** Tickets with events past their record mark, limited to what the viewer may see. */
function pending(c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>, state: Record<string, unknown>, all = false) {
  const marks = recorded(state)
  const out: { key: string; events: number; last: string }[] = []
  for (const key of c.store.ticketKeys(c.ws)) {
    if (!all && !canSeeTicket(c, key)) continue
    const fresh = c.store.eventsOf(key).filter((e) => e.seq > (marks[key] ?? 0))
    if (fresh.length) out.push({ key, events: fresh.length, last: fresh[fresh.length - 1].at })
  }
  return out.sort((a, b) => a.key.localeCompare(b.key))
}

/** Busy day: most tickets have events that are not recorded yet (about 300 events on the DEMO tickets). */
function seedBusy(ws: string, store: MockStore, rng: Rng) {
  const state = seedBase(ws, store)
  const marks = state.recorded as Record<string, number>
  for (const key of store.ticketKeys(ws).sort()) {
    const last = lastSeq(store, key)
    if (last > 8 && rng.chance(0.65)) marks[key] = last - rng.int(1, Math.min(7, last - 1))
  }
  return state
}

function seedBase(ws: string, store: MockStore) {
  const marks: Record<string, number> = {}
  let i = 0
  for (const key of store.ticketKeys(ws).sort()) {
    const last = lastSeq(store, key)
    const lag = last > 0 && i < SEED_PENDING.length && last >= SEED_PENDING[i] ? SEED_PENDING[i] : 0
    if (lag) i++
    marks[key] = last - lag
  }
  const keys = store.ticketKeys(ws).sort()
  return {
    settings: { ...DEFAULTS },
    recorded: marks,
    history: [
      { hash: 'c41d9e2', at: '2026-10-09T08:00:00Z', by: 'auto-commit', perTicket: spread(keys, 0, 5, 17) },
      { hash: '9b07a3f', at: '2026-10-08T17:30:00Z', by: 'Mara', perTicket: spread(keys, 2, 3, 9) },
      { hash: '5e2f810', at: '2026-10-08T09:15:00Z', by: 'Severin', perTicket: spread(keys, 0, 6, 24) },
    ] satisfies Commit[],
    seq: 3,
    behind: false,
    lastPush: { at: '2026-10-08T17:31:00Z', commit: '9b07a3f', remote: REMOTE } satisfies LastPush,
    pushError: null,
    pushOk: null,
  }
}

registerAddon({
  name: 'records',
  seed: seedBase,
  seedBusy,

  view(state, c) {
    const s = settingsOf(state)
    const rows = pending(c, state).map((p) => ({
      ticket: p.key,
      title: c.store.ticket(p.key)?.title ?? '',
      events: p.events,
      last: fmtWhen(p.last, c.store.now()),
    }))
    const events = rows.reduce((n, r) => n + r.events, 0)
    const err = state.pushError as string | null
    const ok = state.pushOk as string | null
    const last = state.lastPush as LastPush | null
    const all = history(state)
    const pushedAt = last ? all.findIndex((h) => h.hash === last.commit) : -1
    const unpushed = pushedAt < 0 ? all.length : pushedAt // commits newer than the last push (newest first)
    const toRecord = rows.length ? `${plural(events, 'event', 'events')} to record` : 'Everything is recorded'
    // The headline says the one thing that is true now; a rejected push leads (the next step is a pull).
    const summary = err
      ? 'Push rejected · pull first'
      : unpushed || rows.length
        ? `${toRecord} · ${unpushed ? `${plural(unpushed, 'commit', 'commits')} waiting to push` : 'nothing to push'}`
        : 'Everything is recorded and pushed'
    // One line under it: why the push was rejected, or what the last push did. The error itself is shown once, here.
    const statusNote = err
      ? { type: 'markdown', text: 'Another clone pushed to the remote first. Pull its commits, then push again.' }
      : ok
        ? { type: 'alert', tone: 'success', title: ok }
        : { type: 'stack', children: [] }
    // Three steps; the next one is primary. Record needs something pending; Push needs a commit the remote lacks.
    const next = err ? 'pull' : rows.length ? 'commit' : unpushed ? 'push' : null
    const step = (label: string, action: string, line: string, disabled?: string) => ({
      type: 'stack',
      children: [
        { type: 'button', label, action, variant: next === action ? 'primary' : action === 'pull' ? 'ghost' : 'secondary', ...(disabled ? { disabled } : {}) },
        ...(disabled ? [] : [{ type: 'markdown', text: line }]),
      ],
    })
    const actions = {
      type: 'stack',
      direction: 'row',
      children: [
        step('Record changes', 'commit', `Saves the ${plural(events, 'pending event', 'pending events')} as one record commit.`, rows.length ? undefined : 'Nothing pending to record.'),
        step('Push to remote', 'push', `Sends ${plural(unpushed, 'record commit', 'record commits')} to the shared remote.`, err ? 'Pull first: the remote has commits you do not have.' : unpushed ? undefined : 'Nothing to push: the remote has every record commit.'),
        step('Pull', 'pull', err ? 'Brings in the commits another clone pushed. Then push again.' : 'Brings in commits another clone pushed.'),
      ],
    }
    // Each entry as this viewer sees it: counts over the visible tickets only; an entry with none of them is left out.
    const hist = history(state)
      .map((h) => {
        const seen = Object.entries(h.perTicket).filter(([k]) => canSeeTicket(c, k))
        return { hash: h.hash, at: h.at, by: h.by, tickets: seen.length, events: seen.reduce((n, [, e]) => n + e, 0) }
      })
      .filter((h) => h.tickets > 0)
    const marks = recorded(state)
    return {
      settings: s,
      recorded: Object.fromEntries(Object.entries(marks).filter(([k]) => canSeeTicket(c, k))), // the raw map names every ticket
      remote: s.remote,
      rows,
      pendingEvents: events,
      unpushed,
      pendingTickets: rows.length,
      summary,
      statusNote,
      actions,
      historyCount: hist.length,
      history: hist, // overrides the raw entries, which carry the per-ticket breakdown
      lastPush: last,
      lastPushText: last ? `Last push ${last.commit} to ${last.remote}, ${fmtExact(last.at)}` : 'Not pushed yet',
      historyItems: hist.map((h, i) => ({
        title: `${plural(h.tickets, 'ticket', 'tickets')}, ${plural(h.events, 'event', 'events')}`,
        subtitle: `${fmtWhen(h.at, c.store.now())} by ${h.by}`,
        badge: last && h.hash === last.commit ? 'pushed' : i === 0 ? 'latest' : undefined,
        status: last && h.hash === last.commit ? ('ok' as const) : ('idle' as const),
      })),
    }
  },

  actions: {
    commit(ctx) {
      const { state, store } = ctx
      // "Is there anything to record" is judged on what the caller can see; hidden tickets stay pending until someone who can see them commits.
      const mine = pending(ctx, state)
      if (!mine.length) return { ok: true, message: 'Already recorded: nothing new since the last commit.' }
      const all = pending(ctx, state, true) // the commit itself is workspace-wide
      const marks = recorded(state)
      for (const key of store.ticketKeys(ctx.ws)) marks[key] = lastSeq(store, key)
      const seq = ((state.seq as number) ?? 0) + 1
      state.seq = seq
      history(state).unshift({ hash: hashOf(seq), at: store.now(), by: nameOf(ctx), perTicket: Object.fromEntries(all.map((p) => [p.key, p.events])) })
      // The workspace log says it happened (Activity lists it); no ticket keys: a commit is workspace-wide.
      store.appendWs(ctx.ws, { type: 'records.committed', actor: ADDON, person: ctx.viewer, commit: hashOf(seq) }) // no counts: they would tell about tickets a reader cannot see
      return { ok: true, message: `Recorded ${plural(mine.reduce((n, p) => n + p.events, 0), 'event', 'events')} on ${plural(mine.length, 'ticket', 'tickets')} as ${hashOf(seq)}.`, changed: true }
    },
    push(ctx) {
      const { state, store } = ctx
      const s = settingsOf(state)
      if (!s.push) return conflict('records.push_off', 'Push is off in the records settings.', 'An owner turns it on in the Records settings.')
      state.pushOk = null
      if (state.behind) {
        state.pushError = REJECTED
        return conflict('records.push_rejected', `Push rejected: ${REJECTED}`, 'Pull first, then push again.')
      }
      const head = history(state)[0]
      state.pushError = null
      state.lastPush = { at: store.now(), commit: head.hash, remote: s.remote } satisfies LastPush
      state.behind = true // the remote moves on: someone else pushes before the next push
      state.pushOk = `Pushed ${head.hash} to ${s.remote}`
      store.appendWs(ctx.ws, { type: 'records.pushed', actor: ADDON, person: ctx.viewer, commit: head.hash })
      return { ok: true, message: `Pushed ${head.hash}.`, changed: true }
    },
    pull(ctx) {
      const { state, store } = ctx
      const was = state.behind === true
      if (was) store.appendWs(ctx.ws, { type: 'records.pulled', actor: ADDON, person: ctx.viewer })
      state.behind = false
      state.pushError = null
      state.pushOk = null
      return { ok: true, message: was ? 'Pulled. Your records are up to date with the remote.' : 'Already up to date.', changed: true }
    },
    save_settings: ({ state, body }) => {
      state.settings = settingsOf({ settings: body.formData })
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
