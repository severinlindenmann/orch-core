// Derives the ticket document (§7) from definitions + events. Events are the only truth for state (T14).
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
  TicketDocument,
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
  if (s.includes(':')) {
    const [id, session, forPerson] = s.split(':')
    return { kind: 'agent', id, session, for: forPerson, grant: 'gr_' + fnvHex(forPerson, 6) }
  }
  return { kind: 'person', id: s, device: 'd_mac' }
}

export interface DeriveContext {
  gates: Workspace['gates']
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
  const gateApprovals: Record<GateName, GateStatus['approvals']> = { requirements: [], plan: [], verify: [] }
  const gateInvalid: Partial<Record<GateName, { reason: string; at: string }>> = {}
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

  for (const e of events) {
    switch (e.type) {
      case 'ticket.created':
        status = (e.status as Status) ?? 'backlog'
        created = e.at
        break
      case 'status.changed':
        status = e.to as Status
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
        gateApprovals[g].push({
          by: actorLabel(e.actor),
          at: e.at,
          via: (e.via as Via | undefined) ?? 'dashboard',
          presence: (e.presence as Presence | undefined) ?? 'touchid',
          sig_ok: (e.sig_ok as boolean | undefined) ?? true,
        })
        delete gateChanges[g]
        delete gateInvalid[g]
        break
      }
      case 'gate.invalidated': {
        const g = e.gate as GateName
        gateInvalid[g] = { reason: (e.reason as string) ?? 'Gated content changed after approval', at: e.at }
        break
      }
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
        verdict = { result: e.result as 'pass' | 'fail', by: actorLabel(e.actor), at: e.at, text: e.text as string | undefined }
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
  const gates = {} as Record<GateName, GateStatus>
  for (const g of ['requirements', 'plan', 'verify'] as GateName[]) {
    const policy = ctx.gates[g]
    const approvals = gateApprovals[g]
    const changes = gateChanges[g]
    const invalid = gateInvalid[g]
    const gated = gateContent(g, def, finalBody, artifacts)
    gates[g] = {
      state: changes
        ? 'changes_requested'
        : invalid
          ? 'invalidated'
          : approvals.length >= policy.count
            ? 'approved'
            : 'pending',
      approvals,
      needed: policy.count,
      approvers: policy.approvers,
      not: policy.not,
      note: changes?.text,
      reason: invalid?.reason,
      hash: 'sha256:' + fnvHex(def.uid + g + gated.material, 12) + '…',
      covers: gated.covers,
    }
  }

  // Section history: events give the revisions; the live text is always the last one.
  for (const [name, text] of Object.entries(finalBody) as [keyof BodySections, string][]) {
    const list = history[name] ?? []
    if (!list.length || list[list.length - 1].text !== text)
      list.push({ rev: list.length + 1, at: list[list.length - 1]?.at ?? created, by: people.owner ?? 'unknown', text } satisfies SectionRevision)
    history[name] = list
  }

  const openBlocking = questions_state.find((q) => q.state === 'open' && q.blocking)
  const turn = computeTurn(status, people, claim, openBlocking, verdict)

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
    body: finalBody,
    section_history: history,
    head: { seq: last?.seq ?? 0, hash: 'sha256:' + fnvHex(def.uid + (last?.seq ?? 0), 12) + '…' },
    created_at: created,
    updated_at: last?.at ?? created,
    restricted: def.visibility !== 'workspace',
  }
}

/** What a gate hash covers (spec section 5, "Gate hash"), as words plus the material the mock hashes. */
function gateContent(g: GateName, def: TicketDefinition, body: BodySections, artifacts: Artifact[]): { covers: string[]; material: string } {
  switch (g) {
    case 'requirements':
      return {
        covers: ['Section: Requirements', 'Section: Out of scope', `Acceptance criteria (${def.acceptance.length})`, 'Type and size'],
        material: JSON.stringify([body.requirements, body.out_of_scope, def.acceptance, def.type, def.size]),
      }
    case 'plan':
      return {
        covers: ['Section: Plan', `Tasks (${def.tasks.length}): ids, text, verify, proves`, 'Section: Decisions'],
        material: JSON.stringify([body.plan, def.tasks, body.decisions]),
      }
    case 'verify':
      return {
        covers: ['Section: Verification', `Artifacts (${artifacts.length}) by sha256`, 'Acceptance criteria and their evidence'],
        material: JSON.stringify([body.verification, artifacts.map((a) => a.sha256), def.acceptance]),
      }
  }
}

function computeTurn(
  status: Status,
  people: People,
  claim: Claim | null,
  openBlocking: QuestionStatus | undefined,
  verdict: TicketDocument['verdict'],
): Turn {
  if (status === 'done') return { who: 'nobody', why: 'Done' }
  if (openBlocking) return { who: openBlocking.to, why: `Answer ${openBlocking.id}` }
  if (status === 'testing' && !verdict) return { who: people.reviewers[0] ?? people.owner ?? 'nobody', why: 'Verdict needed' }
  if (claim) return { who: `agent:${claim.agent}`, why: 'Working' }
  if (status === 'backlog') return { who: people.owner ?? 'nobody', why: 'Refine' }
  if (status === 'open') return { who: people.owner ?? 'nobody', why: 'Ready to claim' }
  if (status === 'waiting') return { who: people.owner ?? 'nobody', why: 'Waiting' }
  return { who: people.owner ?? 'nobody', why: 'Next step' }
}

/** One-line description of an event for feeds. */
export function describeEvent(e: Pick<OrchEvent, 'type'> & Record<string, unknown>): string {
  switch (e.type) {
    case 'ticket.created':
      return 'created the ticket'
    case 'status.changed':
      return `moved to ${e.to}`
    case 'labels.changed':
      return `labelled ${((e.add as string[] | undefined) ?? []).join(', ')}`
    case 'claim.taken':
      return 'took the claim'
    case 'claim.released':
      return 'released the claim'
    case 'lease.taken':
      return `started ${e.task}`
    case 'task.done':
      return `finished ${e.task}`
    case 'artifact.added':
      return `added ${e.name}`
    case 'question.asked':
      return `asked ${e.question}`
    case 'question.answered':
      return `answered ${e.question}`
    case 'gate.approved':
      return `approved ${e.gate}`
    case 'gate.changes_requested':
      return `requested changes on ${e.gate}`
    case 'verdict.given':
      return `gave verdict: ${e.result}`
    case 'handoff.written':
      return 'wrote a handoff'
    case 'section.edited':
      return `edited ${String(e.section).replace('_', ' ')}`
    case 'gate.invalidated':
      return `invalidated the ${e.gate} approval`
    case 'log.added':
      return 'logged a note'
    case 'github.pr_linked':
      return `linked PR #${e.number}`
    case 'publish.shared':
      return 'shared a secret link'
    case 'publish.decided':
      return `decided to publish: ${e.option}`
    case 'estimate.set':
      return `estimated ${e.points} points`
    case 'github.imported':
      return `imported ${e.external} from GitHub`
    case 'usage.recorded':
      return 'recorded usage'
    default:
      return String(e.type)
  }
}
