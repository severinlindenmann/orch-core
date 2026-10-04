// Unit tests of the plugin's pure logic, runnable without the claude CLI: `node --test plugins/orch-session/test/`.
// The fixtures are real orch-core output (make_fixtures.py); ORCH_SESSION_FIXTURES points at a freshly made set.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'

import {
  OUTDATED,
  READS,
  active,
  answerCommand,
  changes,
  clean,
  loadTickets,
  ordered,
  serial,
  statusLine,
  toTicket,
} from '../hooks/tickets.ts'

const DIR = process.env.ORCH_SESSION_FIXTURES ?? join(import.meta.dirname, 'fixtures')
const json = (name: string) => JSON.parse(readFileSync(join(DIR, name), 'utf8'))
const scenarios: Record<string, string> = json('scenarios.json')
const moves: Record<string, { who: string; what: string; label: string }> = json('moves.json')
const show = (scenario: string) => json(`show-${scenarios[scenario]}.json`)
const ticket = (scenario: string) => toTicket(show(scenario))

// The plugin's runner over the fixtures: answers the read commands, records every argv.
function fixtureRun(calls: string[][], fail: (argv: string[]) => 'reject' | 'exit' | 'garbage' | null = () => null) {
  return async (argv: readonly string[]) => {
    calls.push([...argv])
    const how = fail([...argv])
    if (how === 'reject') throw new Error('cannot start')
    if (how === 'exit') return { exitCode: 1, stdout: '{"error": "OrchError", "message": "boom /home/x/secret"}' }
    if (how === 'garbage') return { exitCode: 0, stdout: 'not json' }
    const [, verb, ...rest] = argv
    if (verb === 'list' && rest.includes('--mine')) return { exitCode: 0, stdout: JSON.stringify(json('list-mine.json')) }
    if (verb === 'show') return { exitCode: 0, stdout: JSON.stringify(json(`show-${rest[0]}.json`)) }
    return { exitCode: 2, stdout: '' }
  }
}

test('whose move is orch-core’s own: the CLI’s move matches its dashboard for every scenario', () => {
  for (const [name, id] of Object.entries(scenarios)) {
    const t = toTicket(json(`show-${id}.json`))
    assert.equal(t.move.who, moves[id].who, `${name} (${id}): who`)
    assert.equal(t.move.what, moves[id].what, `${name} (${id}): what`)
    if (t.move.who !== 'agent') assert.equal(t.move.label, moves[id].label, `${name} (${id}): label`)
  }
})

test('pink is only the human’s move; every move has an icon and a word', () => {
  for (const name of Object.keys(scenarios)) {
    const t = ticket(name)
    assert.equal(t.move.role === 'you', t.move.who === 'you', name)
    assert.ok(t.move.label.length > 0, name)
  }
  assert.equal(ticket('working').move.role, 'info')
  assert.equal(ticket('blocked_by').move.role, 'warn')
  assert.equal(ticket('blocked_by').move.label, 'Blocked by L-0012')
  assert.equal(ticket('re_approve').move.why, 'The plan changed after your approval; the work waits until you approve it again.')
  assert.equal(ticket('re_approve').detail, ticket('re_approve').move.why)
})

test('an orch without `move` shows “update orch-core”, never a guess', () => {
  const { move, ...old } = show('answer')
  const t = toTicket(old)
  assert.deepEqual(t.move, OUTDATED)
  assert.equal(t.move.role === 'you', false)
  assert.equal(statusLine([t]), `○ ${t.id} update orch-core`)
  assert.deepEqual(toTicket({ ...show('answer'), move: { who: 'boss', kind: 'x', label: 'y' } }).move, OUTDATED)
})

test('the views read what the agent does next', () => {
  const w = ticket('working')
  assert.equal(w.detail, 'T2 second')
  assert.deepEqual(w.pr, { label: 'PR #12', url: 'https://github.com/acme/repo/pull/12' })
  assert.deepEqual(ordered(w.tasks).map(x => x.id), ['T2', 'T3', 'T1'])
  assert.equal(ticket('no_tasks').detail, 'No task list yet')
  assert.equal(ticket('ready').detail, 'All tasks closed: testing next')
  assert.equal(ticket('changes').detail, 'Revise the plan: changes requested')
  assert.equal(ticket('blocked_task').detail, 'Next T2 two')
  assert.equal(ticket('epic_child').epic, 'L-0014')
  assert.equal(ticket('follow_up').verdict, 'follow-up')
  assert.equal(ticket('verdict').verdict, null)
})

test('an open question names its recommendation and the command for the human, and counts assumptions', () => {
  const t = ticket('answer')
  assert.equal(t.move.label, 'Answer Q1')
  assert.equal(t.detail, 'Which one?')
  assert.deepEqual(t.question, {
    id: 'Q1',
    text: 'Which one?',
    options: [{ key: 'A', label: 'both' }, { key: 'B', label: 'only one' }],
    recommended: 'A',
    command: `orch answer ${t.id} Q1 A`,
  })
  assert.deepEqual(t.confirm, ['Q2'])
  assert.equal(answerCommand('L-1', 'Q3', null), 'orch answer L-1 Q3 <answer>')
  assert.equal(answerCommand('L-1', 'Q3', "it's both"), `orch answer L-1 Q3 'it'\\''s both'`)
})

test('the status line and the band lead with the human’s move', () => {
  const w = ticket('working')
  const q = ticket('answer')
  const b = ticket('blocked_by')
  assert.equal(statusLine([w, q, b]), `◐ ${w.id} working  ● ${q.id} Answer Q1  ▲ ${b.id} blocked`)
  assert.equal(statusLine([]), undefined)
  assert.equal(active([w, b, q]), q)
  assert.equal(active([b, w]), w)
})

test('text from tickets is shown without control or bidi characters', () => {
  assert.equal(clean('a\u001b[31mb\u0007c‮d⁦e\nf'), 'a[31mbcde f')
  assert.equal(clean('x'.repeat(500)).length, 200)
  const d = show('working')
  const t = toTicket({ ...d, title: 'evil\u001b]8;;http://x\u0007title' })
  assert.equal(t.title, 'evil]8;;http://xtitle')
})

test('a PR link is shown only for an http(s) URL', () => {
  const d = show('working')
  const bad = toTicket({ ...d, meta: { ...d.meta, prs: [{ url: 'file:///etc/passwd' }] } })
  assert.equal(bad.pr, null)
})

test('hand-overs raise one toast each', () => {
  const w = ticket('working')
  const asked = { ...w, move: ticket('answer').move, question: ticket('answer').question, status: 'waiting' }
  assert.deepEqual(changes([w], [w]), [])
  assert.deepEqual(changes([w], [asked]), [`● ${w.id} your move: Answer Q1`])
  assert.deepEqual(changes([asked], [w]), [`✓ ${w.id} Answer Q1: done, the agent continues`])
  assert.deepEqual(changes([w], []), [`○ ${w.id} no longer held by this session`])
  assert.deepEqual(changes([], [w]), [`◐ ${w.id} claimed by this session`])
  const back = { ...w, verdict: 'follow-up' }
  assert.deepEqual(changes([w], [back]), [`▲ ${w.id} verdict: follow-up`])
  const moved = { ...w, status: 'testing', move: ticket('verdict').move }
  assert.deepEqual(changes([w], [moved]), [`● ${w.id} your move: Verdict`])
  const blocked = { ...w, move: ticket('blocked_by').move }
  assert.deepEqual(changes([w], [blocked]), [`▲ ${w.id} Blocked by L-0012`])
})

test('a refresh only reads: orch list --mine and orch show', async () => {
  const calls: string[][] = []
  const got = await loadTickets(fixtureRun(calls))
  assert.ok('tickets' in got)
  assert.equal(got.tickets.length, Object.keys(scenarios).length)
  for (const argv of calls) {
    assert.equal(argv[0], 'orch')
    assert.ok(READS.some(r => r.every((w, i) => argv[i + 1] === w)), `not a read: ${argv.join(' ')}`)
    assert.ok(argv.includes('--json'))
  }
})

test('no workspace, a missing orch, a failure or bad output gives a problem, never a partial list', async () => {
  const noWs = async () => ({ exitCode: 2, stdout: '{"error": "UsageError", "message": "no orchestrator/config.json found in this directory or its parents"}' })
  assert.deepEqual(await loadTickets(noWs), { problem: 'no-workspace' })
  assert.deepEqual(await loadTickets(fixtureRun([], () => 'reject')), { problem: 'missing' })
  const timeout = async () => { throw new Error('still running after 15000 ms') }
  assert.deepEqual(await loadTickets(timeout), { problem: 'timeout' })
  const oneShowFails = fixtureRun([], a => (a[1] === 'show' && a[2] === scenarios.answer ? 'exit' : null))
  assert.deepEqual(await loadTickets(oneShowFails), { problem: 'failed' })
  assert.deepEqual(await loadTickets(fixtureRun([], a => (a[1] === 'show' ? 'garbage' : null))), { problem: 'unreadable' })
})

test('one refresh at a time; calls during one run it once more afterwards', async () => {
  let runs = 0
  let release: () => void = () => {}
  const fn = serial(async () => {
    runs++
    await new Promise<void>(r => (release = r))
  })
  const a = fn()
  const b = fn()
  const c = fn()
  assert.equal(runs, 1)
  release()
  await new Promise(r => setImmediate(r))
  assert.equal(runs, 2)
  release()
  await Promise.all([a, b, c])
  assert.equal(runs, 2)
  const failing = serial(async () => { throw new Error('x') })
  await failing()
  await failing()
})
