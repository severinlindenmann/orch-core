import { canSeeTicket, registerAddon, type AddonCtx } from './registry'

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
  tickets: number
  events: number
  by: string
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

registerAddon({
  name: 'records',
  seed(ws, store) {
    const marks: Record<string, number> = {}
    let i = 0
    for (const key of store.ticketKeys(ws).sort()) {
      const last = lastSeq(store, key)
      const lag = last > 0 && i < SEED_PENDING.length && last >= SEED_PENDING[i] ? SEED_PENDING[i] : 0
      if (lag) i++
      marks[key] = last - lag
    }
    return {
      settings: { ...DEFAULTS },
      recorded: marks,
      history: [
        { hash: 'c41d9e2', at: '2026-10-09T08:00:00Z', tickets: 5, events: 17, by: 'auto-commit' },
        { hash: '9b07a3f', at: '2026-10-08T17:30:00Z', tickets: 3, events: 9, by: 'Mara' },
        { hash: '5e2f810', at: '2026-10-08T09:15:00Z', tickets: 6, events: 24, by: 'Severin' },
      ] satisfies Commit[],
      seq: 3,
      behind: false,
      lastPush: { at: '2026-10-08T17:31:00Z', commit: '9b07a3f', remote: REMOTE } satisfies LastPush,
      pushError: null,
      pushOk: null,
    }
  },

  view(state, c) {
    const s = settingsOf(state)
    const rows = pending(c, state).map((p) => ({
      ticket: p.key,
      title: c.store.ticket(p.key)?.title ?? '',
      events: p.events,
      last: p.last.slice(0, 16).replace('T', ' ') + ' UTC',
    }))
    const events = rows.reduce((n, r) => n + r.events, 0)
    const summary = rows.length ? `${plural(events, 'event', 'events')} on ${plural(rows.length, 'ticket', 'tickets')} not recorded yet` : 'Everything is recorded'
    const err = state.pushError as string | null
    const ok = state.pushOk as string | null
    const pushAlert = err
      ? { type: 'alert', tone: 'error', title: err, text: 'Another clone pushed first. Pull, then push again.' }
      : ok
        ? { type: 'alert', tone: 'success', title: ok }
        : { type: 'stack', children: [] }
    const last = state.lastPush as LastPush | null
    const hist = history(state)
    const marks = recorded(state)
    return {
      settings: s,
      recorded: Object.fromEntries(Object.entries(marks).filter(([k]) => canSeeTicket(c, k))), // the raw map names every ticket
      remote: s.remote,
      rows,
      pendingEvents: events,
      pendingTickets: rows.length,
      summary,
      pushAlert,
      lastPush: last,
      lastPushText: last ? `Last push ${last.commit} to ${last.remote}, ${last.at.slice(0, 16).replace('T', ' ')} UTC` : 'Not pushed yet',
      historyItems: hist.map((h, i) => ({
        title: `${h.hash} ${plural(h.tickets, 'ticket', 'tickets')}, ${plural(h.events, 'event', 'events')}`,
        subtitle: `${h.at.slice(0, 16).replace('T', ' ')} UTC by ${h.by}`,
        badge: last && h.hash === last.commit ? 'pushed' : i === 0 ? 'latest' : undefined,
        status: last && h.hash === last.commit ? ('ok' as const) : ('idle' as const),
      })),
    }
  },

  actions: {
    commit(ctx) {
      const { state, store } = ctx
      const all = pending(ctx, state, true)
      if (!all.length) return { ok: true, message: 'Nothing to record.' }
      const marks = recorded(state)
      for (const key of store.ticketKeys(ctx.ws)) marks[key] = lastSeq(store, key)
      const seq = ((state.seq as number) ?? 0) + 1
      state.seq = seq
      history(state).unshift({ hash: hashOf(seq), at: store.now(), tickets: all.length, events: all.reduce((n, p) => n + p.events, 0), by: nameOf(ctx) })
      return { ok: true, message: `Recorded as ${hashOf(seq)}.`, changed: true }
    },
    push(ctx) {
      const { state, store } = ctx
      const s = settingsOf(state)
      if (!s.push) return { ok: true, message: 'Push is off in the records settings.' }
      state.pushOk = null
      if (state.behind) {
        state.pushError = REJECTED
        return { ok: true, message: REJECTED, changed: true }
      }
      const head = history(state)[0]
      state.pushError = null
      state.lastPush = { at: store.now(), commit: head.hash, remote: s.remote } satisfies LastPush
      state.behind = true // the remote moves on: someone else pushes before the next push
      state.pushOk = `Pushed ${head.hash} to ${s.remote}`
      return { ok: true, message: `Pushed ${head.hash}.`, changed: true }
    },
    pull(ctx) {
      const { state } = ctx
      const was = state.behind === true
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
