// Owner decisions 2026-10-10, items 1 and 6: the verdict signs the commit; a new commit voids it; the opt-in code
// review gate; the factory charter gives verdicts (never a code review); landing uses the signed commit.
import { describe, expect, it } from 'vitest'
import { afterEach, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { describeEvent } from './derive'
import { createMockStore } from './store'
import { signedSource } from './addons/land-worker'
import { installAndGrant } from '@/test/installAddon'

const KEY = 'DEMO-0041' // testing, no verdict; Severin is a reviewer
const setup = (viewer = 'p_sev', factory = false) => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  if (factory) installAndGrant(store, ws, 'factory')
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
const head = (s: S, key = KEY) => s.store.ticket(key)!.branch.head
const pass = (s: S, key = KEY, sha = head(s, key)) => s.api.postAction(key, { action: 'verdict', result: 'pass', source_sha: sha })
const codeOn = (s: S, applies: 'all' | string[] = 'all') =>
  s.store.appendWs(s.ws, { type: 'gate.policy_set', gate: 'code', approvers: 'maintainer', count: 1, not: 'assignees', applies })

describe('the verdict signs the commit', () => {
  it('a ticket carries its branch: head, commits and a diffstat that matches core\'s diff', async () => {
    const s = setup()
    const t = s.store.ticket(KEY)!
    expect(t.branch).toMatchObject({ name: 'feat/DEMO-0041-reconciliation', base: 'develop', head: 'c90e7a1' })
    expect(t.branch.commits.map((c) => c.sha)).toEqual(['5be3d10', 'c90e7a1'])
    const ch = await s.api.getChanges(KEY)
    expect(ch).toMatchObject({ head: t.branch.head, additions: t.branch.additions, deletions: t.branch.deletions })
    expect(ch.files).toHaveLength(t.branch.files)
    expect(t.gates.verify.covers![0]).toBe(`Commit c90e7a1 on feat/DEMO-0041-reconciliation: +${t.branch.additions} −${t.branch.deletions} in ${t.branch.files} file${t.branch.files === 1 ? '' : 's'} against develop`)
  })

  it('the hash covers the commit: another head, another verify hash', () => {
    const s = setup()
    const before = s.store.ticket(KEY)!.gates.verify.hash
    s.store.pushCommit(KEY)
    expect(s.store.ticket(KEY)!.gates.verify.hash).not.toBe(before)
  })

  it('records source_sha on the verdict and the verify approval; a verdict on another commit is refused', async () => {
    const s = setup()
    await expect(pass(s, KEY, 'deadbee')).rejects.toMatchObject({ status: 409, code: 'verdict.stale' })
    await pass(s)
    const t = s.store.ticket(KEY)!
    expect(t.verdict).toMatchObject({ result: 'pass', source_sha: 'c90e7a1' })
    expect(t.gates.verify).toMatchObject({ state: 'approved', source_sha: 'c90e7a1' })
    expect(s.store.eventsOf(KEY).filter((e) => e.type === 'verdict.given' || e.type === 'gate.approved').slice(-2).map((e) => e.source_sha)).toEqual(['c90e7a1', 'c90e7a1'])
    expect(t.status).toBe('done')
  })

  it('a new commit after the verdict: core voids it (new_commits), the ticket goes back to testing and the verdict is asked again', async () => {
    const s = setup()
    await pass(s)
    const r = s.store.pushCommit(KEY)
    if (!r.ok) throw new Error(r.message)
    const evs = s.store.eventsOf(KEY).slice(-3)
    expect(evs[0]).toMatchObject({ type: 'branch.pushed', sha: r.sha, actor: { kind: 'agent' } })
    expect(evs[1]).toMatchObject({ type: 'gate.invalidated', gate: 'verify', cause: 'new_commits', sha: r.sha, reason: `New commits after the verdict: ${r.sha}`, actor: { kind: 'host' } })
    expect(evs[2]).toMatchObject({ type: 'status.changed', to: 'testing', actor: { kind: 'host' } })
    const t = s.store.ticket(KEY)!
    expect(t).toMatchObject({ status: 'testing', verdict: null })
    expect(t.gates.verify).toMatchObject({ state: 'invalidated', reason: `New commits after the verdict: ${r.sha}` })
    expect(t.gates.verify.voided?.[0].source_sha).toBe('c90e7a1')
    expect(t.branch.head).toBe(r.sha)
    expect(describeEvent(evs[1])).toBe(`invalidated the verify approval: new commits after it (${r.sha})`)
    expect((await s.api.getToday(s.ws)).needs_you.some((i) => i.kind === 'verdict' && i.ticket === KEY)).toBe(true)
  })

  it('a commit before any verdict voids nothing; the demo push is for members, not viewers', async () => {
    const s = setup()
    s.store.pushCommit(KEY)
    expect(s.store.eventsOf(KEY).at(-1)!.type).toBe('branch.pushed')
    s.store.setViewer('p_tom')
    await expect(s.api.simulatePush(KEY)).rejects.toMatchObject({ status: 403 })
  })
})

describe('the code review gate (opt-in)', () => {
  it('is off by default: in the gate list, not required, and the verdict alone makes the ticket done', () => {
    const s = setup()
    expect(s.store.workspaces.find((w) => w.id === s.ws)!.gates.code).toMatchObject({ applies: 'off', not: 'assignees' })
    expect(s.store.ticket(KEY)!.gates.code).toMatchObject({ required: false, state: 'pending' })
  })

  it('on per ticket type: only those types need it', () => {
    const s = setup()
    codeOn(s, ['bug'])
    expect(s.store.ticket(KEY)!.gates.code.required).toBe(false) // a feature
    codeOn(s, ['feature', 'bug'])
    expect(s.store.ticket(KEY)!.gates.code.required).toBe(true)
  })

  it('on: after a pass verdict the ticket waits in testing for a code review of exactly that commit; then done', async () => {
    const s = setup()
    codeOn(s)
    await pass(s)
    let t = s.store.ticket(KEY)!
    expect(t).toMatchObject({ status: 'testing', turn: { why: 'Code review needed' } })
    expect((await s.api.listTickets(s.ws)).find((x) => x.key === KEY)!.awaiting_gate).toBe('code')
    expect((await s.api.getToday(s.ws)).needs_you.find((i) => i.ticket === KEY && i.ref === 'code')).toMatchObject({ kind: 'approval', text: `Review the code: commit ${t.branch.head}.` })
    expect(t.gates.code.covers![0]).toMatch(/^Commit c90e7a1 on /)
    await expect(s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: 'deadbee' })).rejects.toMatchObject({ status: 409, code: 'gate.stale' })
    await s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: t.branch.head })
    t = s.store.ticket(KEY)!
    expect(t.status).toBe('done')
    expect(t.gates.code).toMatchObject({ state: 'approved', source_sha: t.gates.verify.source_sha })
  })

  it('never by an assignee, and not before a verdict', async () => {
    const s = setup()
    codeOn(s)
    await expect(s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: head(s) })).rejects.toMatchObject({ status: 409, code: 'gate.not_open' })
    await pass(s)
    const assignee = s.store.ticket(KEY)!.people.assignees[0] ?? 'p_mara'
    if (!s.store.ticket(KEY)!.people.assignees.length) s.store.append(KEY, { type: 'people.set', ...s.store.ticket(KEY)!.people, assignees: [assignee] })
    s.store.setViewer(assignee)
    await expect(s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: head(s) })).rejects.toMatchObject({ status: 403, code: 'gate.not_eligible' })
  })

  it('changes asked in the review void the verdict and send the ticket back', async () => {
    const s = setup()
    codeOn(s)
    await pass(s)
    await s.api.postAction(KEY, { action: 'request_changes', gate: 'code', text: 'Rename the column.' })
    expect(s.store.ticket(KEY)).toMatchObject({ status: 'in-progress', verdict: null })
  })

  it('a new commit after the code review voids both approvals', async () => {
    const s = setup()
    codeOn(s)
    await pass(s)
    await s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: head(s) })
    s.store.pushCommit(KEY)
    const t = s.store.ticket(KEY)!
    expect(t.gates.verify.state).toBe('invalidated')
    expect(t.gates.code.state).toBe('invalidated')
    expect(t.status).toBe('testing')
  })
})

describe('the factory charter gives verdicts (never a code review)', () => {
  const child = 'DEMO-0053' // a factory child in testing whose verify approval a landing resolution voided
  const agent = 'claude-code:s_f101:p_sev'
  it('auto-approves the verdict of a child on its branch head, via the factory charter', () => {
    const s = setup('p_sev', true)
    const r = s.store.autoApprove(child, 'verify', { charter: 'factory', by: agent })
    expect(r.ok).toBe(true)
    const t = s.store.ticket(child)!
    expect(t.verdict).toMatchObject({ result: 'pass', via: 'factory_charter', charter: 'DEMO-0050', charter_signed_by: 'p_sev', source_sha: t.branch.head })
    expect(t.gates.verify.approvals.at(-1)).toMatchObject({ via: 'factory_charter', source_sha: t.branch.head })
    expect(t.gates.verify.approvals.at(-1)!.presence).toBeUndefined()
    expect(t.status).toBe('done')
  })

  it('with the code review on for factory tickets, the child waits for a person; the charter cannot approve the review', () => {
    const s = setup('p_sev', true)
    codeOn(s, ['feature', 'bug', 'chore'])
    expect(s.store.autoApprove(child, 'verify', { charter: 'factory', by: agent }).ok).toBe(true)
    expect(s.store.ticket(child)!.status).toBe('testing')
    expect(s.store.autoApprove(child, 'code' as 'verify', { charter: 'factory', by: agent })).toMatchObject({ ok: false, status: 403, code: 'human_only' })
  })

  it('a person is never auto-approved, and a ticket outside the epic is refused', () => {
    const s = setup('p_sev', true)
    expect(s.store.autoApprove(child, 'verify', { charter: 'factory', by: 'p_sev' })).toMatchObject({ ok: false, code: 'charter.agent_only' })
    expect(s.store.autoApprove(KEY, 'verify', { charter: 'factory', by: agent })).toMatchObject({ ok: false, code: 'charter.out_of_scope' })
  })

  it('the factory page names how each verdict was given', async () => {
    const s = setup('p_sev', true)
    const st = (await s.api.getAddonState(s.ws, 'factory')) as { children: { ticket: string; verdict: string }[] }
    expect(st.children.find((c) => c.ticket === 'DEMO-0051')!.verdict).toMatch(/^via the factory charter — no person reviewed this \(commit [0-9a-f]{7}\)$/)
    expect(st.children.find((c) => c.ticket === 'DEMO-0052')!.verdict).toBe('pass by Mara')
  })
})

describe('landing uses the signed commit', () => {
  it("the queue entry's source_sha is the verdict's commit, not inferred", async () => {
    const s = setup()
    await pass(s)
    expect(signedSource(s.store, KEY)).toBe('c90e7a1')
    await s.api.runAddonAction(s.ws, 'land', 'enqueue', { ticket: KEY })
    expect(s.store.eventsOf(KEY).at(-1)).toMatchObject({ type: 'land.queued', source_sha: 'c90e7a1' })
  })

  it('with the code review on, a ticket with only a verdict does not enter the queue', async () => {
    const s = setup()
    codeOn(s)
    await pass(s)
    await expect(s.api.runAddonAction(s.ws, 'land', 'enqueue', { ticket: KEY })).rejects.toMatchObject({ status: 409, code: 'land.not_approved' })
  })

  it('a landing resolution voids the code review too (D53)', async () => {
    const s = setup()
    codeOn(s)
    await pass(s)
    await s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: head(s) })
    s.store.append(KEY, { type: 'land.attempt', actor: 'addon:land', attempt: 99, outcome: 'failed', reason: 'conflict' })
    s.store.append(KEY, { type: 'land.resolved', actor: 'addon:land', attempt: 99, kind: 'conflict' })
    expect(s.store.landingResolved(KEY, { addon: 'land', attempt: 99 }).ok).toBe(true)
    const t = s.store.ticket(KEY)!
    expect(t.gates.verify.state).toBe('invalidated')
    expect(t.gates.code.state).toBe('invalidated')
    expect(t.status).toBe('testing')
  })
})

describe('review fixes', () => {
  const codeOn2 = (s: S, count = 2) =>
    s.store.appendWs(s.ws, { type: 'gate.policy_set', gate: 'code', approvers: 'maintainer', count, not: 'assignees', applies: 'all' })

  it('a code review with count 2 waits for the second reviewer, then the ticket is done', async () => {
    const s = setup()
    codeOn2(s)
    await pass(s, KEY, head(s))
    await s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: head(s) })
    expect(s.store.ticket(KEY)).toMatchObject({ status: 'testing', gates: { code: { state: 'pending' } } })
    s.store.setViewer('p_mara')
    await s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: head(s) })
    expect(s.store.ticket(KEY)).toMatchObject({ status: 'done', gates: { code: { state: 'approved' } } })
  })

  it('a push voids a partial code quorum: an approval on the old commit never counts with one on the new', async () => {
    const s = setup()
    codeOn2(s)
    await pass(s)
    await s.api.postAction(KEY, { action: 'approve', gate: 'code', source_sha: head(s) })
    s.store.pushCommit(KEY)
    expect(s.store.eventsOf(KEY).some((e) => e.type === 'gate.invalidated' && e.gate === 'code' && e.cause === 'new_commits')).toBe(true)
    expect(s.store.ticket(KEY)!.gates.code.approvals).toHaveLength(0)
  })

  it('a push voids a partial verify quorum (count 2): only approvals of the head count', () => {
    const s = setup()
    s.store.appendWs(s.ws, { type: 'gate.policy_set', gate: 'verify', approvers: 'maintainer', count: 2 })
    const old = head(s)
    s.store.append(KEY, { type: 'gate.approved', actor: 'p_mara', gate: 'verify', source_sha: old })
    expect(s.store.ticket(KEY)!.gates.verify.state).toBe('pending')
    s.store.pushCommit(KEY)
    expect(s.store.ticket(KEY)!.gates.verify.approvals).toHaveLength(0)
    // Even without a void, an approval on an older commit is not counted toward the head.
    s.store.append(KEY, { type: 'gate.approved', actor: 'p_sev', gate: 'verify', source_sha: old })
    s.store.append(KEY, { type: 'gate.approved', actor: 'p_mara', gate: 'verify', source_sha: old })
    expect(s.store.ticket(KEY)!.gates.verify.state).toBe('pending')
  })

  it('a re-pushed commit already on the branch does not double the diff', () => {
    const s = setup()
    const before = s.store.ticket(KEY)!.branch
    s.store.append(KEY, { type: 'branch.pushed', actor: 'claude-code:s_x:p_sev', sha: before.head })
    expect(s.store.ticket(KEY)!.branch).toMatchObject({ head: before.head, additions: before.additions, files: before.files })
  })

  it('request changes on verify is refused while a verdict stands', async () => {
    const s = setup()
    codeOn2(s, 1)
    await pass(s)
    await expect(s.api.postAction(KEY, { action: 'request_changes', gate: 'verify', text: 'x' })).rejects.toMatchObject({ status: 409, code: 'verdict.exists' })
  })

  it('turning the code review on moves back only done tickets with an open landing; others count as landed', async () => {
    const s = setup()
    await pass(s)
    expect(s.store.ticket(KEY)!.status).toBe('done')
    const all = { count: 1, applies: 'all' as const }
    // No landing record (merged by hand, a pull request, no landing addon): stays done. In the seed only DEMO-0052's
    // landing is open (queued), so only it would move; DEMO-0042 (pull request) and the landed ones stay done.
    expect(await s.api.previewCodeReview(s.ws, all)).toEqual({ back: { keys: ['DEMO-0052'], hidden: 0 }, done: { keys: [], hidden: 0 } })
    await s.api.runAddonAction(s.ws, 'land', 'enqueue', { ticket: KEY })
    expect(s.store.ticket(KEY)!.landing).toMatchObject({ state: 'queued' })
    expect((await s.api.previewCodeReview(s.ws, all)).back.keys).toEqual([KEY, 'DEMO-0052'])
    await s.api.postSettings(s.ws, { op: 'gate.policy', gate: 'code', approvers: 'maintainer', count: 1, not: 'assignees', applies: 'all' })
    expect(s.store.ticket(KEY)!.status).toBe('testing')
    expect(s.store.ticket('DEMO-0042')!.status).toBe('done')
    expect((await s.api.previewCodeReview(s.ws, { count: 1, applies: 'off' })).done.keys).toEqual([KEY, 'DEMO-0052'])
    await s.api.postSettings(s.ws, { op: 'gate.policy', gate: 'code', approvers: 'maintainer', count: 1, not: 'assignees', applies: 'off' })
    expect(s.store.ticket(KEY)!.status).toBe('done')
  })

  it('a re-push of an older sha (A, B, then A again) is not a new head and voids nothing', async () => {
    const s = setup()
    const a = head(s)
    const r = s.store.pushCommit(KEY)
    if (!r.ok) throw new Error(r.message)
    await pass(s) // on B
    s.store.append(KEY, { type: 'branch.pushed', actor: 'claude-code:s_x:p_sev', sha: a })
    expect(s.store.ticket(KEY)!.branch.head).toBe(r.sha)
    expect(s.store.eventsOf(KEY).some((e) => e.type === 'gate.invalidated' && e.cause === 'new_commits')).toBe(false)
  })

  it('a charter child\'s size is the one recorded at creation: no ticket action edits it', async () => {
    const s = setup()
    await expect(s.api.postAction(KEY, { action: 'set_size', size: 'xs' } as never)).rejects.toMatchObject({ status: 400, code: 'validation' })
  })
})

describe('the charter covers children up to its size only', () => {
  it('refuses a verdict for a child above size m, and the demo skips it', () => {
    const s = setup('p_sev', true)
    const def = (s.store as unknown as { defs: Map<string, { size: string | null }> }).defs.get('DEMO-0053')!
    def.size = 'l'
    expect(s.store.autoApprove('DEMO-0053', 'verify', { charter: 'factory', by: 'claude-code:s_f101:p_sev' })).toMatchObject({ ok: false, status: 409, code: 'charter.out_of_scope' })
    def.size = null
    expect(s.store.autoApprove('DEMO-0053', 'verify', { charter: 'factory', by: 'claude-code:s_f101:p_sev' })).toMatchObject({ ok: false, code: 'charter.out_of_scope' })
  })
})

describe('the factory demo skips children above the charter size', () => {
  afterEach(() => vi.useRealTimers())
  it('gives no verdict to a child of size l waiting in testing', async () => {
    vi.useFakeTimers()
    const s = setup('p_sev', true)
    const def = (s.store as unknown as { defs: Map<string, { size: string | null }> }).defs.get('DEMO-0053')!
    def.size = 'l'
    await s.api.runAddonAction(s.ws, 'factory', 'watch', {})
    vi.advanceTimersByTime(20_000 * 3)
    expect(s.store.ticket('DEMO-0053')!.verdict).toBeNull()
    expect(s.store.eventsOf('DEMO-0053').some((e) => e.type === 'verdict.given' && e.via === 'factory_charter')).toBe(false)
  })
  it('control: within the size limit the demo gives the charter verdict', async () => {
    vi.useFakeTimers()
    const s = setup('p_sev', true)
    await s.api.runAddonAction(s.ws, 'factory', 'watch', {})
    vi.advanceTimersByTime(20_000 * 3)
    expect(s.store.ticket('DEMO-0053')!.verdict).toMatchObject({ via: 'factory_charter' })
  })
})
