// Derives the ticket document (§7) from definitions + events. Events are the only truth for state (T14).
import { codeReviewApplies, commitCover, DEFAULT_CODE_POLICY, GATE_ORDER, gateSignedContent } from '@/api/gates'
import { branchOf, commitOf, firstCommit, type Commit } from './changes'
import type {
  AcceptanceStatus,
  Actor,
  Artifact,
  BodySections,
  Claim,
  GateName,
  GateStatus,
  OrchEvent,
  People,
  QuestionDef,
  QuestionStatus,
  SectionRevision,
  Presence,
  Via,
  Status,
  TaskStatus,
  TicketDefinition,
  TicketBranch,
  TicketDocument,
  TicketLanding,
  Turn,
  Workspace,
} from '@/api/types'

export function fnvHex(input: string, len = 12): string {
  let h1 = 0x811c9dc5
  let h2 = 0x01000193
  for (let i = 0; i < input.length; i++) {
    h1 = Math.imul(h1 ^ input.charCodeAt(i), 0x01000193) >>> 0
    h2 = Math.imul(h2 + input.charCodeAt(i), 0x85ebca6b) >>> 0
  }
  return (h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0')).slice(0, len)
}

export function actorLabel(a: Actor): string {
  if (a.kind === 'person') return a.id
  if (a.kind === 'agent' || a.kind === 'addon') return a.id
  return 'orch'
}

export function parseActor(s: string): Actor {
  if (s === 'host') return { kind: 'host', id: 'orch' }
  // `addon:<name>`: an addon writing its own events (fixtures use it for land.* records).
  if (s.startsWith('addon:')) return { kind: 'addon', id: s.slice(6) }
  if (s.includes(':')) {
    const [id, session, forPerson] = s.split(':')
    return { kind: 'agent', id, session, for: forPerson, grant: 'gr_' + fnvHex(forPerson, 6) }
  }
  return { kind: 'person', id: s, device: 'd_mac' }
}

export interface DeriveContext {
  gates: Workspace['gates']
  /** The policy the workspace started with: an approval that records no `needed` was given under it. */
  seedGates?: Workspace['gates']
  /** The clock. A claim whose `expires` has passed has lapsed (its grant is over): the ticket shows no claim. */
  now: string
}

const EMPTY_PEOPLE: People = { owner: null, assignees: [], reviewers: [], watchers: [] }

export function deriveTicket(
  def: TicketDefinition,
  body: BodySections,
  events: OrchEvent[],
  ctx: DeriveContext,
): TicketDocument {
  let status: Status = 'backlog'
  let people = EMPTY_PEOPLE
  let claim: Claim | null = null
  let verdict: TicketDocument['verdict'] = null
  let handoff: string | null = null
  const artifacts: Artifact[] = []
  const gateApprovals: Record<GateName, GateStatus['approvals']> = { requirements: [], plan: [], verify: [], code: [] }
  // How many approvals the policy asked for when the latest one was given: a later policy change does not undo it.
  const gateNeeded: Partial<Record<GateName, number>> = {}
  const gateInvalid: Partial<Record<GateName, { reason: string; at: string }>> = {}
  // Approvals an invalidation voided: kept for the record, never counted again.
  const gateVoided: Record<GateName, GateStatus['approvals']> = { requirements: [], plan: [], verify: [], code: [] }
  // The branch (D53): commits from task receipts and agent pushes. A verdict and a code review sign its head then.
  const commits: Commit[] = []
  const headNow = () => commits[commits.length - 1]?.sha ?? firstCommit(def, events[0]?.at ?? '', 'unknown').sha
  const history: NonNullable<TicketDocument['section_history']> = {}
  const gateChanges: Partial<Record<GateName, { text?: string; at: string }>> = {}
  const taskDone = new Map<string, { exit: number; ms: number; commit?: string; at: string }>()
  const taskBlocked = new Set<string>()
  const taskSkipped = new Set<string>()
  const leases = new Map<string, { session: string; agent: string; since: string }>()
  const asked = new Map<string, { at: string; by: string }>()
  const answers = new Map<string, NonNullable<QuestionStatus['answer']>>()
  const extraQuestions: QuestionDef[] = []
  let created = events[0]?.at ?? ''
  let labels = def.labels
  // Landing records (D53) count only when written by the addon the event type names (`land.*` by addon land).
  let landing: TicketLanding | undefined
  const landingAddon = (e: OrchEvent) => (e.actor.kind === 'addon' && e.type.startsWith(`${e.actor.id}.`) ? e.actor.id : null)

  for (const e of events) {
    const c = commitOf(e)
    // A commit already on the branch (a re-push of the same sha) adds nothing.
    if (c && !commits.some((x) => x.sha === c.sha)) commits.push(c)
    switch (e.type) {
      case 'ticket.created':
        status = (e.status as Status) ?? 'backlog'
        created = e.at
        break
      case 'status.changed':
        status = e.to as Status
        // Leaving done ends the landing story; a later verdict starts a new one.
        if (status !== 'done') landing = undefined
        break
      case 'labels.changed':
        labels = [...new Set([...labels, ...((e.add as string[] | undefined) ?? [])])].filter((l) => !((e.remove as string[] | undefined) ?? []).includes(l))
        break
      case 'people.set':
        people = {
          owner: (e.owner as string) ?? null,
          assignees: (e.assignees as string[]) ?? [],
          reviewers: (e.reviewers as string[]) ?? [],
          watchers: (e.watchers as string[]) ?? [],
        }
        break
      case 'claim.taken':
        if (e.actor.kind === 'agent')
          claim = {
            agent: e.actor.id,
            session: e.actor.session,
            for: e.actor.for,
            grant: e.actor.grant ?? '',
            since: e.at,
            expires: (e.expires as string) ?? e.at,
          }
        break
      case 'claim.released':
        claim = null
        break
      case 'lease.taken':
        if (e.actor.kind === 'agent') leases.set(e.task as string, { session: e.actor.session, agent: e.actor.id, since: e.at })
        break
      case 'lease.released':
        leases.delete(e.task as string)
        break
      case 'task.done':
        taskDone.set(e.task as string, { ...(e.receipt as { exit: number; ms: number; commit?: string }), at: e.at })
        leases.delete(e.task as string)
        taskBlocked.delete(e.task as string)
        break
      case 'task.blocked':
        taskBlocked.add(e.task as string)
        break
      case 'task.skipped':
        taskSkipped.add(e.task as string)
        break
      case 'artifact.added':
        artifacts.push({
          name: e.name as string,
          kind: e.kind as Artifact['kind'],
          bytes: e.bytes as number,
          sha256: e.sha256 as string,
          task: e.task as string | undefined,
          ac: e.ac as string | undefined,
          label: e.label as string | undefined,
          preview: e.preview as string | undefined,
          url: e.url as string | undefined,
          addon: e.addon as string | undefined,
          ref: e.ref as string | undefined,
          added_by: (e.addon as string | undefined) ?? actorLabel(e.actor),
          at: e.at,
        })
        break
      case 'question.asked':
        asked.set(e.question as string, { at: e.at, by: actorLabel(e.actor) })
        if (e.def) extraQuestions.push(e.def as QuestionDef)
        break
      case 'question.answered':
        answers.set(e.question as string, {
          option: e.option as string | undefined,
          text: e.text as string | undefined,
          by: actorLabel(e.actor),
          at: e.at,
          via: (e.via as Via | undefined) ?? 'dashboard',
          presence: (e.presence as Presence | undefined) ?? 'touchid',
        })
        break
      case 'gate.approved': {
        const g = e.gate as GateName
        gateNeeded[g] = typeof e.needed === 'number' ? e.needed : ctx.seedGates?.[g].count
        gateApprovals[g].push({
          by: actorLabel(e.actor),
          at: e.at,
          via: (e.via as Via | undefined) ?? 'dashboard',
          ...(e.via === 'factory_charter' ? {} : { presence: (e.presence as Presence | undefined) ?? 'touchid' }),
          sig_ok: (e.sig_ok as boolean | undefined) ?? true,
          // verify and code sign a commit: the one the event names, else (older records) the branch head then.
          ...(g === 'verify' || g === 'code' ? { source_sha: (e.source_sha as string | undefined) ?? headNow() } : {}),
        })
        delete gateChanges[g]
        delete gateInvalid[g]
        break
      }
      case 'gate.invalidated': {
        const g = e.gate as GateName
        gateInvalid[g] = { reason: (e.reason as string) ?? 'Gated content changed after approval', at: e.at }
        // The voided approvals no longer count toward the policy: the new content needs a full quorum again.
        gateVoided[g] = [...gateVoided[g], ...gateApprovals[g]]
        gateApprovals[g] = []
        // The verdict is the verify approval: once that approval is void (e.g. a landing conflict was resolved, so the
        // code changed), the verdict no longer stands and the ticket waits for a new one.
        if (g === 'verify') verdict = null
        break
      }
      case 'land.queued': {
        const addon = landingAddon(e)
        if (addon) landing = { state: 'queued', addon, at: e.at }
        break
      }
      case 'land.attempt': {
        const addon = landingAddon(e)
        if (!addon) break
        if (e.outcome === 'failed') landing = { state: 'failed', addon, attempt: e.attempt as number, reason: ((e.reason as TicketLanding['reason']) ?? 'conflict'), at: e.at }
        else if (e.outcome === 'requeued') landing = { state: 'queued', addon, attempt: e.attempt as number, at: e.at }
        else landing = undefined // merged: landed
        break
      }
      case 'land.dequeued':
      case 'land.resolved':
        if (landingAddon(e)) landing = undefined
        break
      case 'section.edited': {
        const sec = e.section as keyof BodySections
        const list = (history[sec] ??= [])
        list.push({ rev: list.length + 1, at: e.at, by: actorLabel(e.actor), text: e.text as string })
        break
      }
      case 'gate.changes_requested': {
        const g = e.gate as GateName
        gateChanges[g] = { text: e.text as string | undefined, at: e.at }
        gateApprovals[g] = []
        break
      }
      case 'verdict.given':
        verdict = {
          result: e.result as 'pass' | 'fail',
          by: actorLabel(e.actor),
          at: e.at,
          text: e.text as string | undefined,
          source_sha: (e.source_sha as string | undefined) ?? headNow(),
          ...(e.via === 'factory_charter' ? { via: 'factory_charter' as const, charter: e.charter as string, charter_signed_by: e.charter_signed_by as string } : {}),
        }
        break
      case 'handoff.written': {
        handoff = e.text as string
        const list = (history.current_state ??= [])
        list.push({ rev: list.length + 1, at: e.at, by: actorLabel(e.actor), text: handoff })
        break
      }
      default:
        break
    }
  }

  const tasks_state: TaskStatus[] = def.tasks.map((t) => {
    const done = taskDone.get(t.id)
    const lease = leases.get(t.id) ?? null
    const state: TaskStatus['state'] = done
      ? 'done'
      : taskSkipped.has(t.id)
        ? 'skipped'
        : taskBlocked.has(t.id)
          ? 'blocked'
          : lease
            ? 'doing'
            : 'todo'
    return {
      ...t,
      state,
      lease,
      receipt: done ? { exit: done.exit, ms: done.ms, commit: done.commit } : null,
      done_at: done?.at ?? null,
    }
  })

  const acceptance_state: AcceptanceStatus[] = def.acceptance.map((ac) => {
    const evidence: AcceptanceStatus['evidence'] = []
    for (const t of tasks_state) if (t.state === 'done' && t.proves.includes(ac.id)) evidence.push({ kind: 'task', ref: t.id })
    for (const a of artifacts) if (a.ac === ac.id) evidence.push({ kind: 'artifact', ref: a.name })
    return { ...ac, state: evidence.length ? 'proven' : 'unproven', evidence }
  })

  const allQuestions = [...def.questions, ...extraQuestions]
  const questions_state: QuestionStatus[] = allQuestions.map((q) => {
    const a = asked.get(q.id)
    const answer = answers.get(q.id) ?? null
    return {
      ...q,
      state: answer ? 'answered' : 'open',
      hash: 'sha256:' + fnvHex(def.uid + q.id + q.text + JSON.stringify(q.options ?? []), 12) + '…',
      asked_at: a?.at ?? created,
      asked_by: a?.by ?? (people.owner ?? 'unknown'),
      answer,
    }
  })

  const finalBody: BodySections = { ...body, current_state: handoff ?? body.current_state }
  const branch = branchOf(def, commits.length ? commits : [firstCommit(def, created, people.owner ?? 'unknown')])
  const gates = {} as Record<GateName, GateStatus>
  for (const g of GATE_ORDER) {
    const policy = ctx.gates[g] ?? DEFAULT_CODE_POLICY
    const approvals = gateApprovals[g]
    const changes = gateChanges[g]
    const invalid = gateInvalid[g]
    const gated = gateContent(g, def, finalBody, artifacts, branch)
    const signed = approvals[approvals.length - 1]?.source_sha
    // verify and code count only approvals of the branch head: one on an older commit never adds to the quorum.
    const counted = g === 'verify' || g === 'code' ? approvals.filter((a) => a.source_sha === branch.head).length : approvals.length
    gates[g] = {
      state: changes
        ? 'changes_requested'
        : invalid
          ? 'invalidated'
          : counted >= policy.count || (counted > 0 && counted >= (gateNeeded[g] ?? Infinity))
            ? 'approved'
            : 'pending',
      approvals,
      needed: policy.count,
      approvers: policy.approvers,
      not: policy.not,
      note: changes?.text,
      reason: invalid?.reason,
      ...(gateVoided[g].length ? { voided: gateVoided[g] } : {}),
      hash: 'sha256:' + fnvHex(def.uid + g + gated.material, 12) + '…',
      covers: gated.covers,
      ...(signed ? { source_sha: signed } : {}),
      ...(g === 'code' ? { required: codeReviewApplies(policy.applies, def.type) } : {}),
    }
  }

  // Section history: events give the revisions; the live text is always the last one.
  for (const [name, text] of Object.entries(finalBody) as [keyof BodySections, string][]) {
    const list = history[name] ?? []
    if (!list.length || list[list.length - 1].text !== text)
      list.push({ rev: list.length + 1, at: list[list.length - 1]?.at ?? created, by: people.owner ?? 'unknown', text } satisfies SectionRevision)
    history[name] = list
  }

  if (claim && claim.expires <= ctx.now) claim = null // lapsed: nobody holds the ticket any more

  const openBlocking = questions_state.find((q) => q.state === 'open' && q.blocking)
  // Landing only applies to a done ticket (a resolution sends it back to testing).
  if (status !== 'done') landing = undefined
  const turn = computeTurn(status, people, claim, openBlocking, verdict, {
    landing,
    codeReview: gates.code.required && gates.code.state !== 'approved',
    plan: gates.plan.state,
    requirements: gates.requirements.state,
    planApprovers: gates.plan.approvers,
    taskCount: def.tasks.length,
  })

  const last = events[events.length - 1]
  return {
    ...def,
    labels,
    status,
    people,
    claim,
    tasks_state,
    acceptance_state,
    questions_state,
    gates,
    artifacts,
    verdict,
    turn,
    branch,
    body: finalBody,
    section_history: history,
    head: { seq: last?.seq ?? 0, hash: 'sha256:' + fnvHex(def.uid + (last?.seq ?? 0), 12) + '…' },
    created_at: created,
    updated_at: last?.at ?? created,
    restricted: def.visibility !== 'workspace',
    ...(landing ? { landing } : {}),
  }
}

/** What a gate hash covers (spec section 5, "Gate hash"), as words plus the material the mock hashes. */
function gateContent(g: GateName, def: TicketDefinition, body: BodySections, artifacts: Artifact[], branch: TicketBranch): { covers: string[]; material: string } {
  switch (g) {
    case 'requirements':
    case 'plan': {
      const { covers, material } = gateSignedContent(g, { ...def, body })
      return { covers, material }
    }
    // The verdict signs the commit too (owner decision 2026-10-10): source_sha is the branch head at verdict time.
    case 'verify':
      return {
        covers: [commitCover(branch), 'Section: Verification', `Artifacts (${artifacts.length}) by sha256`, 'Acceptance criteria and their evidence'],
        material: JSON.stringify([branch.head, body.verification, artifacts.map((a) => a.sha256), def.acceptance]),
      }
    // The code review signs exactly that commit and its diff against the base (the landing worker re-checks the candidate).
    case 'code':
      return {
        covers: [commitCover(branch), `The diff of ${branch.name} against ${branch.base}, file by file`],
        material: JSON.stringify([branch.head, branch.base, branch.files, branch.additions, branch.deletions]),
      }
  }
}

export function computeTurn(
  status: Status,
  people: People,
  claim: Claim | null,
  openBlocking: QuestionStatus | undefined,
  verdict: TicketDocument['verdict'],
  {
    landing,
    codeReview = false,
    plan = 'approved',
    requirements = 'approved',
    planApprovers = 'maintainer',
    taskCount = 1,
  }: {
    landing?: TicketLanding
    /** A pass verdict stands and the code review gate applies and is not approved yet. */
    codeReview?: boolean
    plan?: GateStatus['state']
    requirements?: GateStatus['state']
    planApprovers?: GateStatus['approvers']
    taskCount?: number
  } = {},
): Turn {
  if (status === 'done' && landing) return { who: 'nobody', why: landing.state === 'failed' ? 'Landing failed' : 'Landing' }
  if (status === 'done') return { who: 'nobody', why: 'Done' }
  if (openBlocking) return { who: openBlocking.to, why: `Answer ${openBlocking.id}` }
  if (status === 'testing' && !verdict) return { who: people.reviewers[0] ?? people.owner ?? 'nobody', why: 'Verdict needed' }
  if (status === 'testing' && verdict?.result === 'pass' && codeReview) return { who: people.owner ?? people.reviewers[0] ?? 'nobody', why: 'Code review needed' }
  if (claim) return { who: `agent:${claim.agent}`, why: 'Working' }
  if (status === 'backlog') return { who: people.owner ?? 'nobody', why: 'Refine' }
  // A plan that still needs a signature is the next step, not "ready to claim" (and it names who may sign it).
  if (status === 'open' && requirements === 'approved' && plan !== 'approved' && taskCount > 0) return { who: people.owner ?? 'nobody', why: `Plan needs approval (${planApprovers === 'reviewers' ? 'reviewers' : planApprovers === 'owner' ? 'owners' : 'owners and maintainers'})` }
  if (status === 'open') return { who: people.owner ?? 'nobody', why: 'Ready to claim' }
  if (status === 'waiting') return { who: people.owner ?? 'nobody', why: 'Waiting' }
  return { who: people.owner ?? 'nobody', why: 'Next step' }
}

/** Why core refused an agent, in plain words (agent.refused codes). */
const REFUSAL_WORDS: Record<string, string> = {
  human_only: 'only people approve',
  'claim.held': 'another session holds the ticket',
  'lease.held': 'another session holds that task',
  'gate.not_approved': 'the plan is not approved yet',
  'verify.failed': "the task's check failed",
  'grant.scope': 'its grant does not cover that',
  'grant.expired': 'its grant has ended',
  'grant.revoked': 'its grant was revoked',
}

/** One-line description of an event for feeds. */
export function describeEvent(e: Pick<OrchEvent, 'type'> & Record<string, unknown>): string {
  // A one-line, never-empty summary. Sparse events fall back to a plain phrase instead of "undefined" or a dangling verb.
  const t = (v: unknown, fallback: string): string => (typeof v === 'string' || typeof v === 'number') && String(v).trim() ? String(v).trim() : fallback
  const list = (v: unknown): string[] => (Array.isArray(v) ? v.map((x) => String(x)).filter(Boolean) : [])
  const who = t(e.who ?? e.name ?? e.person, 'a member')
  switch (e.type) {
    case 'ticket.created':
      return 'created the ticket'
    case 'status.changed':
      return e.to ? `moved to ${t(e.to, 'a new status')}` : 'changed the status'
    case 'labels.changed': {
      const add = list(e.add)
      const remove = list(e.remove)
      if (add.length && remove.length) return `labelled ${add.join(', ')}, removed ${remove.join(', ')}`
      if (add.length) return `labelled ${add.join(', ')}`
      if (remove.length) return `removed label ${remove.join(', ')}`
      return 'changed the labels'
    }
    case 'claim.taken':
      return 'took the claim'
    case 'claim.released':
      return e.reason ? `released the claim (${t(e.reason, '')})` : 'released the claim'
    case 'lease.released':
      return e.reason ? `released ${t(e.task, 'a task')} (${t(e.reason, '')})` : `released ${t(e.task, 'a task')}`
    case 'agent.refused':
      // In plain words, never the code: the code stays in the event (Raw) for tools.
      return `was refused: ${REFUSAL_WORDS[String(e.code)] ?? t(e.message, 'no reason given')}`
    case 'lease.taken':
      return `started ${t(e.task, 'a task')}`
    case 'task.done':
      return `finished ${t(e.task, 'a task')}`
    case 'task.run': {
      const exit = (e.receipt as { exit?: unknown } | undefined)?.exit
      if (typeof exit !== 'number') return `ran the check of ${t(e.task, 'a task')}`
      return exit === 0 ? `ran the check of ${t(e.task, 'a task')}: passed` : `ran the check of ${t(e.task, 'a task')}: failed (exit ${exit})`
    }
    case 'artifact.added':
      return `added ${t(e.name, 'an artifact')}`
    case 'question.asked':
      return `asked ${t(e.question, 'a question')}`
    case 'question.answered':
      return `answered ${t(e.question, 'a question')}`
    case 'gate.approved':
      return `approved ${t(e.gate, 'a gate')}`
    case 'gate.changes_requested':
      return `requested changes on ${t(e.gate, 'a gate')}`
    case 'verdict.given':
      return `gave verdict: ${t(e.result, 'none')}`
    case 'handoff.written':
      return 'wrote a handoff'
    case 'section.edited':
      return `edited ${t(e.section, 'a section').replace('_', ' ')}`
    case 'gate.invalidated':
      return e.cause === 'new_commits' ? `invalidated the ${t(e.gate, 'gate')} approval: new commits after it (${t(e.sha, 'a commit')})` : `invalidated the ${t(e.gate, 'gate')} approval`
    case 'branch.pushed':
      return `pushed ${t(e.sha, 'a commit')} to the ticket branch`
    case 'log.added':
      return 'logged a note'
    case 'comment.added':
      return 'commented'
    case 'people.set':
      return 'changed the people on the ticket'
    case 'github.pr_linked':
      return e.number ? `linked PR #${t(e.number, '')}` : 'linked a pull request'
    case 'github.imported':
      return `imported ${t(e.external, 'an issue')} from GitHub`
    case 'publish.shared':
      return 'shared a secret link'
    case 'publish.revoked':
      return 'revoked a share'
    case 'drop.shared':
      return `dropped ${t(e.name, 'a file')}`
    case 'drop.claimed':
      return `claimed ${t(e.name, 'a file')} from the Drop inbox`
    case 'drop.extended':
      return `extended the drop of ${t(e.name, 'a file')}`
    case 'drop.removed':
      return `removed ${t(e.name, 'a file')} from the Drop inbox`
    case 'drop.revoked':
      return `revoked the drop of ${t(e.name, 'a file')}`
    case 'publish.decided':
      return `decided to publish: ${t(e.option, 'no option')}`
    case 'estimate.set':
      return e.points !== undefined ? `estimated ${t(e.points, '?')} points` : 'set an estimate'
    case 'usage.recorded':
      return 'recorded usage'
    case 'quick.made_ticket':
      return 'made a quick task into a ticket'
    case 'addon.decided':
      return `decided ${t(e.option, 'an option')} on a ${t(e.name, 'an addon')} decision`
    case 'addon.action_signed':
      return `signed ${t(e.action, 'an action')} of ${t(e.name, 'an addon')}`
    case 'factory.permit_granted':
      return `granted ${t(e.permit, 'a permit')} ${e.scope === 'epic' ? (e.standing ? 'for this epic (standing grant)' : 'for this epic') : 'once'}`
    case 'factory.permit_refused':
      return `refused ${t(e.permit, 'a permit')}`
    case 'factory.paused':
      return 'paused the AI Factory'
    case 'factory.resumed':
      return 'resumed the AI Factory'
    case 'records.committed':
      return `recorded the ticket records as ${t(e.commit, 'a commit')} (${who})`
    case 'records.pushed':
      return `pushed ${t(e.commit, 'the record commits')} to the remote (${who})`
    case 'records.pulled':
      return `pulled the remote's record commits (${who})`
    case 'wiki.linked':
      return 'linked a wiki page'
    case 'land.queued':
      return `queued for landing on ${t(e.target, 'the target')}`
    case 'land.dequeued':
      return 'took it off the landing queue'
    case 'land.attempt':
      return e.outcome === 'merged'
        ? `landed on ${t(e.target, 'the target')} as ${t(e.candidate_sha, 'a candidate')}`
        : e.outcome === 'requeued'
          ? `rebuilt the landing candidate: ${t(e.target, 'the target')} moved`
          : `landing attempt failed: ${e.reason === 'conflict' ? 'conflict' : 'red checks'}`
    case 'land.resolved':
      return `resolved the landing ${e.kind === 'red_checks' ? 'red checks' : 'conflict'}; the approval is void`
    // Workspace events
    case 'member.added':
      return `added ${who} as ${t(e.role, 'member')}`
    case 'member.role_changed':
      return `made ${who} ${/^[aeiou]/.test(t(e.role, 'member')) ? 'an' : 'a'} ${t(e.role, 'member')}`
    case 'member.removed':
      return `removed ${t(e.who ?? e.name, 'a member')}`
    case 'gate.policy_set':
      return `changed the ${t(e.gate, 'gate')} approval rule`
    case 'addon.installed':
      return `installed ${t(e.name, 'an addon')}`
    case 'addon.granted':
      return `granted ${t(e.name, 'an addon')}`
    case 'addon.enabled':
      return `enabled ${t(e.name, 'an addon')}`
    case 'addon.disabled':
      return `disabled ${t(e.name, 'an addon')}`
    case 'addon.updated':
      return e.version ? `updated ${t(e.name, 'an addon')} to ${t(e.version, '')}` : `updated ${t(e.name, 'an addon')}`
    case 'addon.uninstalled':
      return `uninstalled ${t(e.name, 'an addon')}`
    case 'addon.settings_saved':
      return `saved the settings of ${t(e.name, 'an addon')}`
    case 'grant.issued':
      return 'issued a grant'
    case 'grant.revoked':
      return 'revoked a grant'
    case 'agent.started':
      return e.agent === 'codex' ? 'started an agent session (Codex)' : e.agent === 'claude-code' ? 'started an agent session (Claude Code)' : 'started an agent session'
    case 'agent.stopped':
      return e.reason ? `stopped an agent session (${t(e.reason, '')})` : 'stopped an agent session'
    case 'view.saved':
      return e.name ? `saved view "${t(e.name, '')}"` : 'saved a view'
    case 'view.deleted':
      return 'deleted a saved view'
    case 'ticket.discarded':
      return e.key ? `discarded ${t(e.key, '')} right after creating it` : 'discarded a ticket right after creating it'
    case 'workspace.renamed':
      return e.name ? `renamed the workspace to ${t(e.name, '')}` : 'renamed the workspace'
    case 'terminal.shell_opened':
      return `opened a shell as ${t(e.run_as, 'the agent user')} to log ${t(e.connection, 'a connection')} in again`
    case 'workspace.grant_hours_set':
      return typeof e.hours === 'number' ? `set the agent grant length to ${e.hours} h` : 'set the agent grant length'
    case 'skill.credentials_granted': {
      const refs = [...(Array.isArray(e.connections) ? e.connections : []), ...(Array.isArray(e.env) ? e.env : [])].map((x) => t(x, '')).filter(Boolean)
      return `granted ${refs.length ? refs.join(', ') : 'credentials'} to the skill ${t(e.skill, '')}`.trimEnd()
    }
    case 'connection.checked': {
      const status = (e.result as { status?: unknown } | undefined)?.status
      return `checked ${t(e.name, 'a connection')}: ${t(typeof status === 'string' ? status.replace(/_/g, ' ') : '', 'unknown')}`
    }
    default:
      return t(e.type, 'did something')
  }
}
