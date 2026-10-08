// One generated ticket: a definition, sections and a plausible event history for each archetype (Arch).
// The histories use the same event types as the fixtures, so the store derives everything the usual way.
import { fnvHex } from '../derive'
import {
  AC_TEXTS, ASKS, COMMENTS, LABELS, MODELS, OPTION_PAIRS, PERSON_NAME, REPOS, TASK_CMDS, TASK_VERBS, VERDICT_TEXTS, WHYS,
  longTitle, pickTitle,
} from './pools'
import type { Rng } from './rng'
import { ENDED, GRANT_OF, ROOTS, actorOf, subsOf, type Session } from './roster'
import { HOUR, MIN, NOW_MS, stamp, type Draft } from './timeline'
import type { Arch, Built, GenDefinition, WsCfg } from './types'

export interface Plan {
  arch: Arch
  n: number
  type?: GenDefinition['type']
  parent?: string | null
  restricted?: string[]
  longTitle?: boolean
  /** Children of an epic are small. */
  small?: boolean
}

const POINTS = { xs: 1, s: 2, m: 3, l: 5, xl: 8 } as const
const wpick = <T>(rng: Rng, pairs: [T, number][]): T => {
  const total = pairs.reduce((n, [, w]) => n + w, 0)
  let x = rng.next() * total
  for (const [v, w] of pairs) if ((x -= w) < 0) return v
  return pairs[pairs.length - 1][0]
}

/** What the later passes share while the tickets of one workspace are built. */
export interface BuildState {
  rng: Rng
  cfg: WsCfg
  /** Next live root session that takes a claim (round robin). */
  claimer: number
  /** Next pull request number per repo. */
  pr: Record<string, number>
}
export const newBuildState = (rng: Rng, cfg: WsCfg): BuildState => ({ rng, cfg, claimer: 0, pr: { 'acme-energy-dbt': 210, 'acme-energy-billing-api': 140, 'acme-energy-ingest': 60 } })

const keyOf = (cfg: WsCfg, n: number) => `${cfg.prefix}-${String(n).padStart(4, '0')}`
const bullet = (lines: string[]) => lines.map((l) => `- ${l}`).join('\n')
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 30).replace(/-+$/, '')

/** When a ticket was created and when it last moved, for its archetype. */
function window(rng: Rng, arch: Arch): [number, number] {
  const ago = (loDays: number, hiDays: number) => NOW_MS - Math.floor(rng.int(loDays * 24, hiDays * 24) * HOUR)
  const recent = (loMin: number, hiMin: number) => NOW_MS - rng.int(loMin, hiMin) * MIN
  let created: number
  let last: number
  switch (arch) {
    case 'done':
      created = ago(5, 21)
      last = rng.chance(0.3) ? recent(15, 600) : created + Math.floor((NOW_MS - created) * (0.25 + rng.next() * 0.65))
      break
    case 'testSev': case 'testMara': case 'testBoth': case 'testNobody': case 'testFailed':
      created = ago(3, 14)
      last = rng.chance(0.6) ? recent(10, 400) : recent(24 * 60, 40 * 60)
      break
    case 'wip':
      created = ago(1, 10)
      last = recent(3, 120)
      break
    case 'qSev': case 'qBoth': case 'qMara':
      created = ago(1, 9)
      last = recent(20, 26 * 60)
      break
    case 'reqPending':
      created = ago(0.5, 4)
      last = recent(60, 40 * 60)
      break
    case 'planPending': case 'qOwner': case 'qAssignee':
      created = ago(1, 6)
      last = recent(60, 30 * 60)
      break
    case 'openReady':
      created = ago(1, 12)
      last = recent(2 * 60, 72 * 60)
      break
    case 'backlogNew':
      created = ago(1, 20)
      last = Math.min(recent(30, 96 * 60), created + rng.int(2, 60) * HOUR)
      break
    case 'waitingExt':
      created = ago(3, 14)
      last = recent(5 * 60, 60 * 60)
      break
    case 'epic':
      created = ago(10, 21)
      last = recent(60, 30 * 60)
      break
  }
  last = Math.min(last, NOW_MS - 90_000)
  created = Math.min(created, last - 3 * HOUR)
  return [created, last]
}

export function buildTicket(st: BuildState, plan: Plan): Built {
  const { rng, cfg } = st
  const { arch } = plan
  const key = keyOf(cfg, plan.n)
  const O = cfg.owner
  const X = cfg.other
  const isEpic = arch === 'epic'
  const type = isEpic ? 'epic' : (plan.type ?? wpick<GenDefinition['type']>(rng, [['feature', 40], ['bug', 22], ['chore', 26], ['spike', 8]]))
  const repo = rng.pick(REPOS)
  const base = pickTitle(rng, type)
  const title = isEpic ? `${rng.pick(['Monthly', 'Quarterly', 'Regional', 'Customer', 'Finance'])} ${rng.pick(['billing', 'reporting', 'metering', 'tariff', 'data quality'])} ${rng.pick(['overhaul', 'programme', 'migration', 'clean-up'])}` : plan.longTitle ? longTitle(rng, base) : base
  const priority = wpick<GenDefinition['priority']>(rng, [['low', 20], ['medium', 45], ['high', 25], ['urgent', 10]])
  const size = isEpic ? null : plan.small ? rng.pick(['xs', 's', 'm'] as const) : wpick<GenDefinition['size']>(rng, [[null, 12], ['xs', 15], ['s', 25], ['m', 25], ['l', 15], ['xl', 8]])
  const labels = rng.sample(LABELS, wpick(rng, [[0, 15], [1, 40], [2, 30], [3, 15]]))

  // ---- people
  const live = cfg.agents
  const dev = (a: Arch) => live && ['wip', 'qSev', 'qBoth', 'qMara', 'waitingExt'].includes(a)
  let holder: Session | null = null
  if (dev(arch) && (arch === 'wip' || rng.chance(0.55))) {
    const pool = arch === 'qMara' ? ROOTS.filter((s) => s.for === 'p_mara') : ROOTS
    holder = pool[st.claimer++ % pool.length]
  }
  let owner = rng.chance(0.65) ? O : X
  let assignees: string[] = [rng.pick([O, X])]
  let reviewers: string[] = rng.chance(0.5) ? [rng.pick([O, X])] : []
  switch (arch) {
    case 'testSev': owner = O; assignees = [X]; reviewers = [O]; break
    case 'testMara': owner = X; assignees = [O]; reviewers = [X]; break
    case 'testBoth': owner = O; assignees = []; reviewers = [O, X]; break
    case 'testNobody': assignees = [rng.pick([O, X])]; reviewers = []; break
    case 'testFailed': assignees = [O]; reviewers = [X]; break
    case 'done': assignees = [rng.pick([O, X])]; reviewers = [assignees[0] === O ? X : O]; break
    case 'qOwner': owner = O; break
    case 'qAssignee': assignees = [X]; break
    case 'epic': owner = O; assignees = []; reviewers = []; break
    default: break
  }
  if (holder) { assignees = [holder.for]; owner = holder.for }
  const watchers = rng.chance(0.2) ? ['p_tom'] : []

  // ---- acceptance, tasks, questions
  const nAc = isEpic ? 2 : rng.int(1, 4)
  const acTexts = rng.sample(AC_TEXTS, nAc)
  const acceptance = acTexts.map((text, i) => ({ id: `AC${i + 1}`, text }))
  const nT = isEpic ? 0 : Math.max(nAc, rng.int(2, 5))
  const models = rng.sample(MODELS, nT)
  const tasks: GenDefinition['tasks'] = Array.from({ length: nT }, (_, i) => ({
    id: `T${i + 1}`,
    text: `${rng.pick(TASK_VERBS)} ${models[i]}`,
    verify: rng.chance(0.75) ? { cmd: rng.pick(TASK_CMDS).replace('{m}', models[i]) } : null,
    proves: i < nAc ? [`AC${i + 1}`] : [],
    ...(rng.chance(0.2) ? { assignee: rng.pick([O, X]) } : {}),
  }))
  const questions: GenDefinition['questions'] = []
  const ask = (to: string, blocking: boolean) => {
    const [a, b] = rng.pick(OPTION_PAIRS)
    const q = {
      id: `Q${questions.length + 1}`,
      to,
      text: rng.pick(ASKS),
      why: rng.pick(WHYS),
      options: [{ key: slug(a) || 'a', label: a }, { key: slug(b) || 'b', label: b, ...(rng.chance(0.3) ? { cost: '+1 day' } : {}) }],
      recommended: slug(a) || 'a',
      blocking,
    }
    questions.push(q)
    return q
  }

  // ---- who does the work
  const ended = rng.pick(ENDED)
  const worker = holder ? actorOf(holder) : cfg.agents && rng.chance(0.8) ? actorOf(ended) : rng.pick([O, X])
  const hasPr = !isEpic && ['done', 'testSev', 'testMara', 'testBoth', 'testNobody', 'testFailed', 'wip'].includes(arch) && rng.chance(0.7)
  const branches = { [repo.name]: `feat/${key}-${slug(title)}` }
  const prs: GenDefinition['links']['prs'] = hasPr ? [{ repo: repo.name, url: `https://github.com/${repo.gh}/pull/${st.pr[repo.name]++}` }] : []

  // ---- the history, in order
  const ev: Draft[] = []
  const add = (type: string, actor: string, extra: Record<string, unknown> = {}) => ev.push({ type, actor, ...extra })
  const comment = (actor: string) => add('log.added', actor, { text: rng.pick(COMMENTS) })
  const comments = (n: number) => { for (let i = 0; i < n; i++) comment(rng.pick([O, X, ...(live ? [worker] : [])])) }
  const doneTasks: string[] = []
  const requirements = bullet(acceptance.map((a) => a.text))
  const plan_ = tasks.map((t, i) => `${i + 1}. ${t.text}`).join('\n')

  const refine = (withQuestion: boolean) => {
    add('section.edited', owner, { section: 'requirements', text: requirements })
    if (rng.chance(0.5)) add('section.edited', owner, { section: 'context', text: `Repo \`${repo.name}\`.` })
    if (rng.chance(0.3)) add('labels.changed', owner, { add: [rng.pick(LABELS)] })
    if (withQuestion) {
      const q = ask(rng.pick([O, X]), false)
      add('question.asked', worker, { question: q.id })
      add('question.answered', q.to, { question: q.id, option: q.recommended, text: 'Go with the recommendation.', via: rng.pick(['dashboard', 'cli', 'phone']), presence: 'touchid' })
    }
    if (rng.chance(0.5)) comments(1)
  }
  const approveReq = () => add('gate.approved', O, { gate: 'requirements', via: 'dashboard', presence: 'touchid' })
  const toOpen = () => add('status.changed', 'host', { to: 'open' })
  const approvePlan = () => {
    add('section.edited', owner, { section: 'plan', text: plan_ })
    add('gate.approved', O, { gate: 'plan', via: rng.pick(['dashboard', 'cli']), presence: 'touchid' })
  }
  // Without agents (INT, CLI) people do the work: no claim, no lease.
  const claim = (actor: string, forPerson: string) => {
    if (live) add('claim.taken', actor, { expires: forPerson === 'p_mara' ? GRANT_OF.p_mara.until : GRANT_OF.p_sev.until })
    add('status.changed', actor, { to: 'in-progress' })
  }
  const runTask = (actor: string, t: GenDefinition['tasks'][number], opts: { fail?: boolean } = {}) => {
    if (live) add('lease.taken', actor, { task: t.id })
    if (t.verify && (opts.fail || rng.chance(0.35)))
      add('task.run', actor, { task: t.id, receipt: { exit: opts.fail ? 1 : 0, ms: rng.int(900, 60_000) }, log: `${t.id.toLowerCase()}-check.log` })
    if (opts.fail) return
    add('task.done', actor, { task: t.id, receipt: { exit: 0, ms: rng.int(900, 90_000), commit: fnvHex(key + t.id, 7) } })
    doneTasks.push(t.id)
  }
  const work = (actor: string, upTo: number) => {
    for (const t of tasks.slice(0, upTo)) {
      runTask(actor, t)
      if (rng.chance(0.15)) comments(1)
    }
    if (prs.length) add('github.pr_linked', actor, { number: Number(prs[0].url.split('/').pop()) })
  }
  const handoff = (actor: string) =>
    add('handoff.written', actor, { text: `${doneTasks.length ? `${doneTasks.join(', ')} done.` : 'Nothing finished yet.'} ${rng.pick(COMMENTS)}` })
  const toTesting = (actor: string) => {
    if (live) add('claim.released', actor)
    add('status.changed', 'host', { to: 'testing' })
  }
  const verdict = (reviewer: string, result: 'pass' | 'fail') => {
    if (result === 'pass') add('gate.approved', reviewer, { gate: 'verify', via: 'dashboard', presence: 'touchid' })
    add('verdict.given', reviewer, { result, text: result === 'pass' ? rng.pick(VERDICT_TEXTS) : 'Two cases are still wrong, see the check output.' })
    if (result === 'pass') add('status.changed', 'host', { to: 'done' })
  }

  let waitingOn: Built['waitingOn'] = null
  add('ticket.created', rng.pick([O, X]), { status: 'backlog' })
  add('people.set', O, { owner, assignees, reviewers, watchers })

  switch (arch) {
    case 'epic':
      refine(false); approveReq(); toOpen(); approvePlan(); comments(rng.int(2, 6)); break
    case 'done':
      refine(rng.chance(0.4)); approveReq(); toOpen(); approvePlan(); claim(worker, holder?.for ?? O); work(worker, nT); handoff(worker); toTesting(worker)
      comments(rng.int(0, 3)); verdict(reviewers[0], 'pass'); break
    case 'testSev': case 'testMara': case 'testBoth': case 'testNobody': case 'testFailed':
      refine(rng.chance(0.4)); approveReq(); toOpen(); approvePlan(); claim(worker, O); work(worker, nT); handoff(worker); toTesting(worker); comments(rng.int(0, 2))
      if (arch === 'testFailed') verdict(reviewers[0], 'fail')
      break
    case 'wip': {
      refine(rng.chance(0.3)); approveReq(); toOpen(); approvePlan()
      const k = rng.int(0, Math.max(0, nT - 1))
      claim(worker, holder?.for ?? O)
      const subs = holder ? subsOf(holder.id) : []
      work(worker, k)
      const leased = tasks.slice(k, k + rng.int(1, Math.min(3, nT - k)))
      if (live) leased.forEach((t, i) => {
        const who = subs.length && rng.chance(0.7) ? actorOf(subs[i % subs.length]) : worker
        add('lease.taken', who, { task: t.id })
      })
      if (rng.chance(0.4)) comments(1)
      break
    }
    case 'qSev': case 'qBoth': case 'qMara': {
      refine(false); approveReq(); toOpen(); approvePlan()
      const k = rng.int(0, Math.max(0, nT - 1))
      if (holder) {
        claim(worker, holder.for); work(worker, k); add('lease.taken', worker, { task: tasks[k].id })
      }
      const to1 = arch === 'qMara' ? X : O
      const q1 = ask(to1, true)
      add('question.asked', worker, { question: q1.id })
      if (arch === 'qBoth') { const q2 = ask(X, false); add('question.asked', worker, { question: q2.id }) }
      add('status.changed', 'host', { to: 'waiting' })
      if (holder) waitingOn = { question: q1.id }
      break
    }
    case 'qOwner': case 'qAssignee': {
      refine(false); approveReq(); toOpen(); approvePlan()
      const q = ask(arch === 'qOwner' ? 'owner' : 'assignee', false)
      add('question.asked', worker, { question: q.id })
      break
    }
    case 'reqPending':
      add('section.edited', owner, { section: 'requirements', text: requirements }); comments(rng.int(1, 2)); add('labels.changed', owner, { add: [rng.pick(LABELS)] })
      break
    case 'planPending':
      refine(false); approveReq(); toOpen(); add('section.edited', owner, { section: 'plan', text: plan_ }); comments(rng.int(0, 2))
      break
    case 'openReady':
      refine(rng.chance(0.3)); approveReq(); toOpen(); approvePlan(); comments(rng.int(0, 2))
      break
    case 'backlogNew':
      add('section.edited', owner, { section: 'summary', text: `${title}.` }); if (rng.chance(0.5)) comments(rng.int(1, 3)); if (rng.chance(0.4)) add('labels.changed', owner, { add: [rng.pick(LABELS)] })
      break
    case 'waitingExt':
      refine(false); approveReq(); toOpen(); approvePlan()
      if (holder) { claim(worker, holder.for); work(worker, Math.min(1, nT)); add('claim.released', worker, { reason: 'waiting for the finance export' }) }
      add('log.added', O, { text: 'Waiting for the finance export; it lands on the 1st.' })
      add('status.changed', 'host', { to: 'waiting' })
      break
  }
  // Keep every history between 5 and 40 events: pad short ones with comments, drop chatter from long ones.
  while (ev.length < 5) comment(rng.pick([O, X]))
  for (const droppable of ['log.added', 'task.run', 'labels.changed']) {
    for (let i = ev.length - 1; i >= 0 && ev.length > 36; i--) if (ev[i].type === droppable && i > 1) ev.splice(i, 1)
  }

  const [createdMs, lastMs] = window(rng, arch)
  const events = stamp(rng, ev, createdMs, lastMs)

  // ---- the definition and sections
  const external = rng.chance(0.08) ? [{ label: `GH-${rng.int(100, 199)}`, url: `https://github.com/${repo.gh}/issues/${rng.int(100, 199)}` }] : []
  const definition: GenDefinition = {
    uid: ('01J9ZN' + fnvHex(key, 16).toUpperCase() + fnvHex(key + 'u', 4).toUpperCase()).slice(0, 26),
    key,
    title,
    type,
    priority,
    size,
    labels,
    parent: plan.parent ?? null,
    ...(plan.restricted ? { visibility: { restricted: plan.restricted } } : {}),
    links: { repos: [repo.name], branches, prs, external },
    acceptance,
    tasks,
    questions,
    addons: {
      ...(size ? { estimate: { points: POINTS[size] } } : {}),
      ...(['done', 'wip', 'testSev', 'testMara', 'testBoth', 'testNobody'].includes(arch) ? { usage: { cents: rng.int(40, 900), sessions: rng.int(1, 4), ms: rng.int(600_000, 9_000_000) } } : {}),
    },
  }
  const body: Record<string, string> = { summary: `${title}. ${rng.pick(WHYS)}` }
  if (arch === 'backlogNew') {
    body.requirements = 'Not refined yet.'
  } else {
    body.context = `${bullet([`Repo: \`${repo.name}\`.`, `Owner: ${PERSON_NAME[owner] ?? owner}.`])}`
    body.requirements = requirements
    if (nT && arch !== 'reqPending') body.plan = plan_
  }
  if (['done', 'testSev', 'testMara', 'testBoth', 'testNobody', 'testFailed'].includes(arch))
    body.verification = bullet(acceptance.map((a) => `${a.id}: ${a.text}, checked in the run of T${a.id.slice(2)}.`))
  if (arch === 'wip') body.current_state = `Working on ${tasks.filter((t) => !doneTasks.includes(t.id)).slice(0, 2).map((t) => t.id).join(' and ')}.`

  return { definition, body, events, arch, worker, holder: holder?.id ?? null, doneTasks, waitingOn }
}

