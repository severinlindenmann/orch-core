import { describe, expect, it } from 'vitest'
import { clean, createShell, replay, runCommand, type ShellCtx } from './fakePty'

const TICKET: NonNullable<ShellCtx['ticket']> = {
  key: 'DEMO-0043',
  title: 'Tariff seeds',
  status: 'in_progress',
  current_state: 'Seeds loaded; reconciliation pending.',
  next_task: { id: 'T3', text: 'Run the reconciliation' },
  move: { who: 'agent:claude-code', why: 'working on T2' },
  gates: [
    { name: 'requirements', state: 'approved' },
    { name: 'plan', state: 'approved' },
    { name: 'verify', state: 'pending' },
  ],
  questions: { open: 1, total: 2 },
  tasks: { done: 2, total: 5, doing: 'T2' },
}
const ctx = (over: Partial<ShellCtx> = {}): ShellCtx => ({
  user: 'severin',
  cwd: '~/energy',
  branch: 'feat/billing-join',
  owner: 'person',
  now: '2026-10-09T11:30:00Z',
  cursor: 41,
  grant: { id: 'gr_01J9Z8', scope: 'all', until: '2026-10-09T18:00:00Z' },
  claim: { agent: 'claude-code', session: 's_77c2', for: 'p_sev', expires: '2026-10-09T12:10:00Z' },
  ticket: { ...TICKET },
  ...over,
})
const first = (line: string, c = ctx()) => runCommand(line, c).lines[0]

describe('runCommand: first line of each command', () => {
  it('orch status prints move, gates, questions, claim, tasks, cursor and the grant from live data', () => {
    expect(runCommand('orch status', ctx()).lines).toEqual([
      'DEMO-0043 · Tariff seeds · in_progress',
      'move       agent:claude-code · working on T2',
      'gates      requirements approved · plan approved · verify pending',
      'questions  1 open of 2',
      'claim      claude-code s_77c2 · for p_sev · expires 12:10 UTC',
      'tasks      2/5 done · doing T2 · next T3',
      'cursor     41',
      'grant      gr_01J9Z8 · all · until 18:00 UTC (6 h 30 min left)',
    ])
  })
  it('orch status reflects missing grant, missing claim and no ticket', () => {
    expect(runCommand('orch status', ctx({ grant: null, claim: null })).lines).toContain('grant      none · run orch grant')
    expect(runCommand('orch status', ctx({ grant: null, claim: null })).lines).toContain('claim      none')
    expect(first('orch status', ctx({ ticket: null }))).toBe('orch · no ticket in this shell')
  })
  it('orch show prints the requested section of the ticket', () => {
    expect(runCommand('orch show DEMO-0043 --section current_state', ctx()).lines).toEqual(['DEMO-0043 · Current state', 'Seeds loaded; reconciliation pending.'])
    expect(first('orch show DEMO-0099 --section current_state')).toBe('err not_found · no ticket DEMO-0099 in this shell')
  })
  it('orch task next prints the next task', () => {
    expect(first('orch task next')).toBe('next T3 · Run the reconciliation')
    expect(first('orch task next', ctx({ ticket: { ...TICKET, next_task: null } }))).toBe('no open tasks')
  })
  it('git, ls, pwd, help, exit', () => {
    expect(first('git status')).toBe('On branch feat/billing-join')
    expect(runCommand('git log --oneline -5', ctx()).lines).toHaveLength(5)
    expect(first('ls')).toMatch(/seeds/)
    expect(first('pwd')).toBe('/Users/severin/energy')
    expect(first('help')).toMatch(/^commands: .*orch status/)
    expect(runCommand('exit', ctx())).toMatchObject({ exit: true, lines: ['logout'] })
    expect(runCommand('clear', ctx()).clear).toBe(true)
  })
  it('unknown commands and blank lines', () => {
    expect(first('frobnicate --now')).toBe('zsh: command not found: frobnicate')
    expect(runCommand('   ', ctx()).lines).toEqual([])
    expect(first('orch frob')).toBe('err unknown_command · orch frob · next: help')
  })
  it('orch approve is refused with human_only in an agent shell and points to the dashboard in a person shell', () => {
    expect(first('orch approve DEMO-0043 plan', ctx({ owner: 'agent', user: 'claude' }))).toBe('err human_only approve · retry:false · next: orch ask or orch wait')
    expect(first('orch approve DEMO-0043 plan', ctx())).toBe('approve needs Touch ID: use the dashboard')
  })
})

describe('untrusted text never reaches the terminal as control sequences', () => {
  // eslint-disable-next-line no-control-regex
  const CONTROL = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/
  const hostile = 'a\x1b]8;;https://evil\x07x\x1b]8;;\x07 \x1b]52;c;ZXZpbA==\x07 \x1b[2K\x1b[1A \x9b31m end'
  it('clean strips ESC and all C0/C1 controls', () => {
    expect(clean(hostile)).not.toMatch(CONTROL)
    expect(clean('\x9b')).toBe('')
    expect(clean('plain text · ünï')).toBe('plain text · ünï')
  })
  it('titles, states, tasks, moves, claims and the prompt are cleaned in every command', () => {
    const t = { ...TICKET, title: hostile, current_state: hostile, next_task: { id: 'T3', text: hostile }, move: { who: hostile, why: hostile } }
    const c = ctx({ ticket: t, branch: hostile, user: hostile, claim: { agent: hostile, session: hostile, for: hostile, expires: '2026-10-09T12:10:00Z' } })
    for (const cmd of ['orch status', 'orch show DEMO-0043 --section current_state', 'orch task next', 'git status']) {
      for (const l of runCommand(cmd, c).lines) expect(l, cmd).not.toMatch(CONTROL)
    }
    expect(replay(c, ['orch status', 'orch task next'])).not.toMatch(/\x1b|[\u0080-\u009f]/)
    const sh = createShell(() => c)
    expect(sh.prompt()).not.toMatch(CONTROL)
    sh.feed('orch status')
    expect(sh.feed('\r').replace(/\r\n/g, '')).not.toMatch(CONTROL)
  })
  it('replay cleans the transcript commands too', () => {
    const out = replay(ctx(), ['pwd\x1b]8;;https://evil\x07x\x1b]8;;\x07\x9b'])
    expect(out).not.toMatch(/\x1b|[\u0080-\u009f]|\x07/)
  })
  it('a multi-line current_state is split into lines written with CRLF (no staircase)', () => {
    const c = ctx({ ticket: { ...TICKET, current_state: 'one\ntwo\r\nthree' } })
    expect(runCommand('orch show DEMO-0043 --section current_state', c).lines).toEqual(['DEMO-0043 · Current state', 'one', 'two', 'three'])
    const sh = createShell(() => c)
    sh.feed('orch show DEMO-0043 --section current_state')
    expect(sh.feed('\r')).toContain('one\r\ntwo\r\nthree\r\n')
  })
  it('typed input with a C1 control byte is ignored', () => {
    const sh = createShell(() => ctx())
    expect(sh.feed('\x9b31m')).toBe('')
  })
})

describe('createShell: line editing, history and Ctrl-C', () => {
  const live = ctx()
  it('has the prompt of the brief', () => {
    expect(createShell(() => live).prompt()).toBe('severin@acme ~/energy (feat/billing-join) $ ')
  })
  it('echoes typing, runs on Enter and prints a new prompt', () => {
    const sh = createShell(() => live)
    expect(sh.feed('pwd')).toBe('pwd')
    expect(sh.feed('\r')).toBe('\r\n/Users/severin/energy\r\nseverin@acme ~/energy (feat/billing-join) $ ')
  })
  it('backspace edits the line', () => {
    const sh = createShell(() => live)
    sh.feed('pwx')
    expect(sh.feed('\x7f')).toBe('\b \b')
    sh.feed('d')
    expect(sh.feed('\r')).toContain('/Users/severin/energy')
  })
  it('arrow up recalls the previous commands, arrow down comes back', () => {
    const sh = createShell(() => live)
    for (const c of ['pwd', 'ls']) {
      sh.feed(c)
      sh.feed('\r')
    }
    expect(sh.history()).toEqual(['pwd', 'ls'])
    expect(sh.feed('\x1b[A')).toContain('ls')
    expect(sh.feed('\x1b[A')).toContain('pwd')
    expect(sh.feed('\x1b[B')).toContain('ls')
    expect(sh.feed('\x1b[B')).not.toContain('ls')
  })
  it('Ctrl-C drops the line and prints a fresh prompt without running it', () => {
    const sh = createShell(() => live)
    sh.feed('rm -rf')
    expect(sh.feed('\x03')).toBe('^C\r\nseverin@acme ~/energy (feat/billing-join) $ ')
    expect(sh.history()).toEqual([])
  })
  it('clear wipes the screen; exit ends the session and ignores further input', () => {
    const sh = createShell(() => live)
    sh.feed('clear')
    expect(sh.feed('\r')).toBe('\x1b[2J\x1b[H' + 'severin@acme ~/energy (feat/billing-join) $ ')
    sh.feed('exit')
    expect(sh.feed('\r')).toContain('logout')
    expect(sh.exited()).toBe(true)
    expect(sh.feed('pwd')).toBe('')
  })
  it('reads live data at command time, not at creation', () => {
    let c = ctx()
    const sh = createShell(() => c)
    c = ctx({ cursor: 42 })
    sh.feed('orch status')
    expect(sh.feed('\r')).toContain('cursor     42')
  })
})

describe('replay (agent mirror transcript)', () => {
  it('prints prompt, command and output for each command', () => {
    const out = replay(ctx({ owner: 'agent', user: 'claude' }), ['orch task next', 'orch approve DEMO-0043 plan'])
    expect(out).toContain('claude@acme ~/energy (feat/billing-join) $ orch task next\r\nnext T3 · Run the reconciliation\r\n')
    expect(out).toContain('err human_only approve · retry:false · next: orch ask or orch wait')
  })
})

describe('createShell: pastes', () => {
  it('a multi-line paste keeps its text (line breaks become spaces, controls are dropped); an escape sequence is ignored', () => {
    const sh = createShell(() => ctx())
    expect(sh.feed('git\r\nstatus\x07')).toBe('git status')
    expect(sh.feed('\x1b]52;c;ZXZpbA==\x07')).toBe('')
    expect(sh.feed('\r')).toContain('On branch')
  })
})

describe('a re-login shell (prefill)', () => {
  it('types the login command at the prompt without running it; Enter runs it', async () => {
    const { openSession } = await import('./harnessView')
    const c = ctx({ user: 'orch-agent', ticket: null })
    const session = { id: 'sh9', harness: 'shell', ctx: c, prefill: 'databricks auth login --profile prod', run_as: 'orch-agent', transcript: [], status: 'running', resumedFrom: null } as unknown as Parameters<typeof openSession>[0] extends () => infer S ? S : never
    const shell = openSession(() => session, () => 80)
    const first = shell.start()
    expect(first).toContain('orch-agent@acme')
    expect(first.endsWith('databricks auth login --profile prod')).toBe(true) // typed, no output after it
    expect(first).not.toContain('Demo')
    expect(shell.feed('\r')).toContain('Demo: no login happens in this mockup')
  })
})
