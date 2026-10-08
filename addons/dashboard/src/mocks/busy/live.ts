// The gentle background script of the busy day (store.sim): one meaningful event every 4 to 8 seconds on a random
// ticket everybody can see (a task done, a comment, a question asked, a claim taken), capped at 300 events per rolling
// hour. It runs only when the store is `live` (the app, not the tests) and the dataset is busy; reset stops it.
import { COMMENTS, ASKS, OPTION_PAIRS, WHYS } from './pools'
import { makeRng, type Rng } from './rng'
import { GRANT_OF, ROOTS, SESSIONS, actorOf } from './roster'
import { BUSY_SEED } from './generate'
import type { MockStore } from '../store'

export const LIVE_ID = 'busy:live'
export const LIVE_CAP_PER_HOUR = 300
const HOUR = 3_600_000

export function startLive(store: MockStore): void {
  const rng = makeRng(BUSY_SEED).fork('live')
  const times: number[] = []
  const tick = (): void =>
    store.sim.play(LIVE_ID, [
      {
        afterMs: rng.int(4000, 8000),
        run: (st) => {
          const now = Date.parse(st.now())
          while (times.length && now - times[0] >= HOUR) times.shift()
          if (times.length <= LIVE_CAP_PER_HOUR - 3) times.push(...liveStep(st, rng).map(() => now))
          tick()
        },
      },
    ])
  tick()
}

/** Plays one step; returns one entry per event appended. */
function liveStep(st: MockStore, rng: Rng): number[] {
  const demo = rng.chance(0.75)
  const ws = st.workspaces.find((w) => (demo ? w.prefix === 'DEMO' : w.prefix !== 'DEMO' && rng.chance(0.6))) ?? st.workspaces[0]
  const isDemo = ws.prefix === 'DEMO'
  // Tickets everybody in the workspace can see and that are not finished.
  const docs = st.ticketKeys(ws.id).flatMap((k) => {
    const t = st.ticket(k)
    return t && !t.restricted && t.status !== 'done' ? [t] : []
  })
  if (!docs.length) return []
  const people = ws.members.filter((m) => m.role !== 'viewer').map((m) => m.person)
  const kind = rng.next()
  if (isDemo && kind < 0.3) {
    const t = rng.shuffle(docs).find((d) => d.tasks_state.some((x) => x.state === 'doing' && x.lease))
    const task = t?.tasks_state.find((x) => x.state === 'doing' && x.lease)
    if (t && task) {
      const s = SESSIONS.find((x) => x.id === task.lease!.session)
      const forPerson = s?.for ?? t.claim?.for ?? 'p_sev'
      st.append(t.key, { type: 'task.done', actor: `${task.lease!.agent}:${task.lease!.session}:${forPerson}`, task: task.id, receipt: { exit: 0, ms: rng.int(900, 90_000), commit: rng.int(0x1000000, 0xfffffff).toString(16) } })
      return [1]
    }
  } else if (kind >= 0.7 && kind < 0.82) {
    const t = rng.shuffle(docs).find((d) => ['open', 'in-progress', 'waiting'].includes(d.status) && !d.questions_state.some((q) => q.state === 'open'))
    if (t) {
      const [a, b] = rng.pick(OPTION_PAIRS)
      const id = `Q${t.questions_state.length + 1}`
      const to = rng.pick(people)
      const actor = isDemo && t.claim ? `${t.claim.agent}:${t.claim.session}:${t.claim.for}` : rng.pick(people)
      st.append(t.key, {
        type: 'question.asked',
        actor,
        question: id,
        def: { id, to, text: rng.pick(ASKS), why: rng.pick(WHYS), options: [{ key: 'a', label: a }, { key: 'b', label: b }], recommended: 'a', blocking: false },
      })
      return [1]
    }
  } else if (isDemo && kind >= 0.82) {
    const t = rng.shuffle(docs).find((d) => d.status === 'open' && !d.claim && d.gates.plan.state === 'approved' && d.tasks.length)
    // A session takes up to five claims at a time.
    const free = ROOTS.filter((s) => docs.filter((d) => d.claim?.session === s.id).length < 5)
    if (t && free.length) {
      const s = rng.pick(free)
      const actor = actorOf(s)
      st.append(t.key, { type: 'claim.taken', actor, expires: GRANT_OF[s.for].until })
      st.append(t.key, { type: 'status.changed', actor, to: 'in-progress' })
      return [1, 1]
    }
  }
  // A comment is always possible.
  const t = rng.pick(docs)
  const worker = isDemo && t.claim ? `${t.claim.agent}:${t.claim.session}:${t.claim.for}` : rng.pick(people)
  st.append(t.key, { type: 'log.added', actor: worker, text: rng.pick(COMMENTS) })
  return [1]
}
