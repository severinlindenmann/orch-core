import { describe, expect, it } from 'vitest'
import { createShell, replay, runCommand, type ShellCtx } from './fakePty'

const ctx = (over: Partial<ShellCtx> = {}): ShellCtx => ({
  user: 'severin',
  cwd: '~/energy',
  branch: 'feat/billing-join',
  owner: 'person',
  now: '2026-10-09T11:30:00Z',
  cursor: 41,
  grant: { id: 'gr_01J9Z8', scope: 'all', until: '2026-10-09T18:00:00Z' },
  claim: { agent: 'claude-code', session: 's_77c2', for: 'p_sev', expires: '2026-10-09T12:10:00Z' },
  ticket: { key: 'DEMO-0043', title: 'Tariff seeds', status: 'in_progress', current_state: 'Seeds loaded; reconciliation pending.', next_task: { id: 'T3', text: 'Run the reconciliation' } },
  ...over,
})
const first = (line: string, c = ctx()) => runCommand(line, c).lines[0]

describe('runCommand: first line of each command', () => {
  it('orch status prints the ticket, the viewer grant, the claim and the cursor from live data', () => {
    const r = runCommand('orch status', ctx())
    expect(r.lines).toEqual([
      'DEMO-0043 · Tariff seeds · in_progress',
      'grant   gr_01J9Z8 · all · until 18:00 UTC (6 h 30 min left)',
      'claim   claude-code s_77c2 · for p_sev · expires 12:10 UTC',
      'cursor  41',
    ])
  })
  it('orch status reflects missing grant, missing claim and no ticket', () => {
    expect(runCommand('orch status', ctx({ grant: null, claim: null })).lines).toContain('grant   none · run orch grant')
    expect(runCommand('orch status', ctx({ grant: null, claim: null })).lines).toContain('claim   none')
    expect(first('orch status', ctx({ ticket: null }))).toBe('orch · no ticket in this shell')
  })
  it('orch show prints the requested section of the ticket', () => {
    expect(runCommand('orch show DEMO-0043 --section current_state', ctx()).lines).toEqual(['DEMO-0043 · Current state', 'Seeds loaded; reconciliation pending.'])
    expect(first('orch show DEMO-0099 --section current_state')).toBe('err not_found · no ticket DEMO-0099 in this shell')
  })
  it('orch task next prints the next task', () => {
    expect(first('orch task next')).toBe('next T3 · Run the reconciliation')
    expect(first('orch task next', ctx({ ticket: { key: 'DEMO-0043', title: 't', status: 'done', current_state: '', next_task: null } }))).toBe('no open tasks')
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
    expect(sh.feed('\r')).toContain('cursor  42')
  })
})

describe('replay (agent mirror transcript)', () => {
  it('prints prompt, command and output for each command', () => {
    const out = replay(ctx({ owner: 'agent', user: 'claude' }), ['orch task next', 'orch approve DEMO-0043 plan'])
    expect(out).toContain('claude@acme ~/energy (feat/billing-join) $ orch task next\r\nnext T3 · Run the reconciliation\r\n')
    expect(out).toContain('err human_only approve · retry:false · next: orch ask or orch wait')
  })
})
