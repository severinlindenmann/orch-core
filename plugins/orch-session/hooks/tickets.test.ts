// Engine tests (`claude plugin test plugins/orch-session`): the hooks as a session runs them. The logic itself is
// tested without the claude CLI in test/tickets.spec.ts, against real orch-core output.
import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

// Trimmed from real `orch show --json` output (test/fixtures): one ticket with an open blocking question.
const SHOW = {
  id: 'L-0002',
  title: 'Question ticket',
  status: 'waiting',
  meta: {
    type: 'feature',
    size: 'xs',
    prs: [],
    blocked_by: [],
    parent: null,
    gates: { verify: { verdict: null } },
    questions: [
      {
        id: 'Q1',
        text: 'Which one?',
        type: 'single',
        options: [{ key: 'A', label: 'both' }, { key: 'B', label: 'only one' }],
        recommended: 'A',
        blocking: true,
        answer: null,
      },
    ],
  },
  sections: { Plan: '1. do it' },
  gates: { requirements: 'approved', plan: 'pending' },
  move: { who: 'you', kind: 'answer', label: 'Answer Q1', ref: 'Q1', why: 'The agent asked Q1 and waits for your answer.' },
  tasks: {
    error: null,
    summary: { total: 1, closed: 0 },
    doing: null,
    next: null,
    open: ['T1'],
    can_move_to_testing: false,
    tasks: [{ id: 'T1', state: 'blocked', text: 'do', owner: 'agent', needs_open: [], why: 'waits for the answer to Q1' }],
  },
}

type World = {
  calls: string[][]
  status: (string | undefined)[]
  toasts: string[]
  fail: boolean
  settle: () => Promise<void>
}

function world(on: On): World {
  const clock = mock.clock(on)
  const w: World = { calls: [], status: [], toasts: [], fail: false, settle: () => clock.settle() }
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('session.id', async () => ({ value: 'session-1' }))
  on('command.register', async () => ({ value: { command: 'orch' } }))
  on('config.list', async () => ({ value: [] }))
  on('ui.status', async ($, e) => {
    w.status.push(e.text)
    return { value: undefined }
  })
  on('ui.toast', async ($, e) => {
    w.toasts.push(e.text)
    return { value: undefined }
  })
  // what the Bash tool answers beneath the plugin (a test registers every hook before its first call on $)
  on('tool.call', { tool: 'Bash' }, async () => ({ result: { stdout: '', stderr: '', interrupted: false }, text: '' }) as never)
  on('process.run', async ($, e) => {
    w.calls.push([...e.argv])
    if (w.fail) return { value: { exitCode: 1, stdout: '{"error": "OrchError", "message": "x"}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    const verb = e.argv[1]
    const stdout = verb === 'list' ? JSON.stringify([{ id: 'L-0002' }]) : JSON.stringify(SHOW)
    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return w
}

test('session start reads this session’s tickets and names the human’s move', async ($, on) => {
  const w = world(on)
  await $.session.start({ cwd: '.', surface: 'terminal', isInteractive: true })
  await w.settle()
  expect(w.calls).toEqual([
    ['orch', 'list', '--mine', '--json'],
    ['orch', 'show', 'L-0002', '--json'],
  ])
  expect(w.status.at(-1)).toBe('● L-0002 Answer Q1')
})

test('a failing read keeps the last good state and raises no toast', async ($, on) => {
  const w = world(on)
  await $.session.start({ cwd: '.', surface: 'terminal', isInteractive: true })
  await w.settle()
  w.fail = true
  await $.tool.call({ tool: 'Bash', command: 'orch task done L-0002 T1' })
  await w.settle()
  expect(w.status.at(-1)).toBe('● L-0002 Answer Q1')
  expect(w.toasts).toEqual([])
})
