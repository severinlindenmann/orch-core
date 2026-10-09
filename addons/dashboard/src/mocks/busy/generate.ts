// The busy-day dataset (Task 31b): the normal demo PLUS generated data, fixture-shaped, deterministic.
// `generateBusy(seed)` is pure (seeded PRNG; no Math.random, no Date.now). The store seeds it through the same path as
// the normal fixtures (tickets and events), the agent registry and grants, and each addon module's `seedBusy`.
import { addArtifacts } from './artifacts'
import { addRefusals, agentRows, grantSessions, type AgentRow } from './agents'
import { makeRng, type Rng } from './rng'
import { buildTicket, newBuildState, type Plan } from './tickets'
import type { Arch, Built, GenTicket, WsCfg } from './types'
import { addWidgets } from './widgets'

export const BUSY_SEED = 20261009

/** The mix of the 128 non-epic tickets of DEMO; the other workspaces get it scaled down. */
const BASE_DECK: [Arch, number][] = [
  ['done', 38], ['testSev', 6], ['testMara', 2], ['testBoth', 2], ['testNobody', 5], ['testFailed', 2],
  ['qSev', 6], ['qOwner', 4], ['qBoth', 4], ['qMara', 2], ['qAssignee', 2],
  ['reqPending', 3], ['planPending', 2], ['wip', 22], ['openReady', 14], ['backlogNew', 10], ['waitingExt', 4],
]
const BASE_TOTAL = BASE_DECK.reduce((n, [, c]) => n + c, 0)

export function deckFor(n: number): Arch[] {
  const counts = BASE_DECK.map(([a, c]) => [a, Math.max(1, Math.round((c * n) / BASE_TOTAL))] as [Arch, number])
  const sum = counts.reduce((t, [, c]) => t + c, 0)
  const done = counts.find(([a]) => a === 'done')!
  done[1] = Math.max(0, done[1] + n - sum)
  return counts.flatMap(([a, c]) => Array<Arch>(c).fill(a))
}

export const WORKSPACES: WsCfg[] = [
  {
    prefix: 'DEMO', first: 100, count: 131, owner: 'p_sev', other: 'p_mara', agents: true,
    restricted: [['p_sev'], ['p_sev'], ['p_sev'], ['p_sev'], ['p_sev', 'p_mara'], ['p_sev', 'p_mara'], ['p_sev', 'p_mara'], ['p_sev', 'p_mara']],
    epics: [18, 12, 9], extraEpic: { key: 'DEMO-0050', children: 16 }, bigEpic: { index: 0, extra: 22 },
  },
  { prefix: 'INT', first: 100, count: 58, owner: 'p_sev', other: 'p_mara', agents: false, restricted: [['p_sev'], ['p_sev'], ['p_sev']], epics: [7] },
  { prefix: 'CLI', first: 100, count: 29, owner: 'p_sev', other: 'p_tom', agents: false, restricted: [['p_sev'], ['p_sev']], epics: [] },
]

/** Who is what in each ticket: archetype, number, restriction, parent, long title. */
function planFor(cfg: WsCfg, rng: Rng): Plan[] {
  const plans: Plan[] = []
  let n = cfg.first
  for (let i = 0; i < cfg.epics.length; i++) plans.push({ arch: 'epic', n: n++ })
  for (const arch of rng.shuffle(deckFor(cfg.count - cfg.epics.length))) plans.push({ arch, n: n++ })
  const key = (num: number) => `${cfg.prefix}-${String(num).padStart(4, '0')}`
  const plain = plans.filter((p) => p.arch !== 'epic')
  // Restricted tickets are quiet ones, so a hidden ticket never hides somebody's open item.
  const quiet = rng.shuffle(plain.filter((p) => ['done', 'openReady', 'backlogNew'].includes(p.arch)))
  cfg.restricted.forEach((list, i) => quiet[i] && (quiet[i].restricted = list))
  for (const p of rng.sample(plain, Math.round(plain.length * 0.1))) p.longTitle = true
  const free = rng.shuffle(plain.filter((p) => !p.restricted))
  cfg.epics.forEach((size, i) => {
    for (const p of free.splice(0, size)) {
      p.parent = key(cfg.first + i)
      p.small = true
    }
  })
  if (cfg.extraEpic)
    for (const p of free.splice(0, cfg.extraEpic.children)) {
      p.parent = cfg.extraEpic.key
      p.small = true
    }
  if (cfg.bigEpic)
    for (const p of free.splice(0, cfg.bigEpic.extra)) {
      p.parent = key(cfg.first + cfg.bigEpic.index)
      p.small = true
    }
  return plans
}

/** Every ticket of one workspace, with the extra facts later passes need. */
export function buildWorkspace(cfg: WsCfg, seed: number): Built[] {
  const rng = makeRng(seed).fork(cfg.prefix)
  const st = newBuildState(rng, cfg)
  const tickets = planFor(cfg, rng).map((p) => buildTicket(st, p))
  addArtifacts(rng.fork('artifacts'), tickets, { max: cfg.prefix === 'DEMO' ? 40 : cfg.prefix === 'INT' ? 8 : 4 })
  if (cfg.agents) {
    addRefusals(rng.fork('refusals'), tickets)
    addWidgets(rng.fork('widgets'), tickets)
  }
  return tickets
}

export interface BusyData {
  /** Generated tickets per workspace prefix, in fixture shape (added to the normal fixtures). */
  tickets: Record<WsCfg['prefix'], GenTicket[]>
  /** Agent session registry rows (like fixtures/me.json `agents`), all in the DEMO workspace. */
  agents: AgentRow[]
  /** Session ids each grant covers, so revoking a grant ends them. */
  grantSessions: Record<string, string[]>
}

const strip = (b: Built): GenTicket => ({ definition: b.definition, body: b.body, events: b.events })

export function generateBusy(seed: number = BUSY_SEED): BusyData {
  const built = Object.fromEntries(WORKSPACES.map((cfg) => [cfg.prefix, buildWorkspace(cfg, seed)])) as Record<WsCfg['prefix'], Built[]>
  return {
    tickets: { DEMO: built.DEMO.map(strip), INT: built.INT.map(strip), CLI: built.CLI.map(strip) },
    agents: agentRows(makeRng(seed).fork('agents'), built.DEMO),
    grantSessions: grantSessions(),
  }
}
