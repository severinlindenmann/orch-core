import { describe, expect, it } from 'vitest'
import type { ShellCtx } from '@/api/terminals'
import { cliScreen, createCli, fitLine, wrap, type CliSession } from './cli'

const ctx = (over: Partial<ShellCtx['ticket'] & object> = {}): ShellCtx => ({
  user: 'claude',
  cwd: '~/energy',
  branch: 'feat/billing-join',
  owner: 'agent',
  now: '2026-10-09T11:30:00Z',
  cursor: 7,
  grant: { id: 'gr_1', scope: 'all', until: '2026-10-09T18:00:00Z' },
  claim: null,
  ticket: {
    key: 'DEMO-0043',
    title: 'Load tariff tables',
    status: 'in_progress',
    current_state: 'T1 done.',
    next_task: { id: 'T2', text: 'Seeds' },
    move: { who: 'p_sev', why: 'Answer Q2' },
    gates: [{ name: 'plan', state: 'approved' }],
    questions: { open: 1, total: 2 },
    tasks: { done: 1, total: 3, doing: 'T2' },
    ...over,
  },
})

const mirror = (over: Partial<CliSession> = {}): CliSession => ({
  harness: 'claude',
  kind: 'agent',
  status: 'running',
  started: '2026-10-09T09:40:00Z',
  ctx: ctx(),
  transcript: ['orch status', 'orch task next', 'orch approve DEMO-0043 plan'],
  context: true,
  summary: null,
  resumedFrom: null,
  ...over,
})

/** The text with this module's own SGR colours and cursor moves removed: what remains must hold no escape at all. */
const plain = (s: string) => s.replace(/\x1b\[[0-9;]*[mAGJK]|\x1b[78]/g, '')
const HOSTILE = 'Seeds \x1b]8;;https://evil.example\x07click\x1b]8;;\x07 \x1b]52;c;ZXZpbA==\x07\x1b[2K\x9b31m end'

describe('Claude Code mirror screen', () => {
  it('has the CLI look: welcome box, the prompt, tool calls with results, a working line, and no input or key hints', () => {
    const s = cliScreen(mirror(), 100)
    const t = plain(s.text)
    expect(t).toContain('Welcome to Claude Code (simulated)')
    expect(t).toMatch(/╭─+╮/)
    expect(t).toContain('> /orch:work DEMO-0043')
    expect(t).toContain('⏺ Bash(orch status)')
    expect(t).toMatch(/ {2}⎿ {2}DEMO-0043 · Load tariff tables/)
    expect(t).toContain('err human_only approve')
    expect(t).toContain('Approving is human-only')
    // A watched agent session has nothing input-shaped and no hint for keys that do nothing here.
    expect(t).not.toMatch(/esc to interrupt|ctrl\+r|for shortcuts|│ >/)
    expect(t.split('\r\n').at(-1)).toMatch(/Waiting for approval…/)
    expect(s.live).toMatchObject({ up: 0 })
    expect(plain(s.live!.frame(3))).toMatch(/Waiting for approval… \(1h 50m\)$/)
  })
  it('cuts long tool output to three lines and says how many more there were', () => {
    const t = plain(cliScreen(mirror({ transcript: ['orch status'] }), 100).text)
    expect(t).toMatch(/… \+\d+ lines\r\n/)
  })
  it('fits tool output to the width between words, never mid-word', () => {
    const t = plain(cliScreen(mirror({ transcript: ['orch status'] }), 40).text).split('\r\n')
    for (const l of t) expect(l.length).toBeLessThanOrEqual(40)
    expect(fitLine('one two three four', 9)).toEqual(['one two', 'three', 'four'])
    expect(fitLine('a  b', 9)).toEqual(['a  b']) // a line that fits keeps its spacing
    expect(fitLine('abcdefghijkl', 5)).toEqual(['abcde', 'fghij', 'kl'])
  })
  it('an ended session ends with its summary and no live line', () => {
    const s = cliScreen(mirror({ status: 'stopped', summary: 'Planned the join.' }), 100)
    expect(plain(s.text)).toContain('Session ended. Summary: Planned the join.')
    expect(s.text).toContain('[process completed]')
    expect(s.live).toBeUndefined()
  })
  it('Codex looks different: its own header, "• Ran" lines and └ results', () => {
    const t = plain(cliScreen(mirror({ harness: 'codex' }), 100).text)
    expect(t).toContain('>_ OpenAI Codex (simulated)')
    expect(t).toContain('• Ran orch status')
    expect(t).toMatch(/└ DEMO-0043/)
    expect(t).not.toContain('⏺')
  })
  it('data-derived text goes through clean(): a hostile title or command leaves no escape on screen', () => {
    const c = ctx({ title: HOSTILE + ' \x1b[5n \x1b[6n' })
    const s = cliScreen(mirror({ ctx: c, transcript: ['orch status', `ls \x1b]52;c;x\x07`], summary: HOSTILE, status: 'stopped' }), 100)
    const t = plain(s.text)
    // plain() only removes this module's own SGR and cursor moves: a device-status query (CSI 5n / 6n) from data would
    // survive it, so finding no ESC proves clean() ran on the data.
    expect(t).not.toMatch(/[\u0000-\u0008\u000b-\u000c\u000e-\u001f\u007f-\u009f]/)
    expect(t).toContain('[5n')
    expect(t).toContain('end')
  })
  it('boxes follow the width', () => {
    const narrow = plain(cliScreen(mirror(), 40).text).split('\r\n')[0]
    const wide = plain(cliScreen(mirror(), 120).text).split('\r\n')[0]
    expect(narrow.length).toBe(39)
    expect(wide.length).toBe(76)
  })
})

describe('your own CLI session (interactive)', () => {
  const open = (over: Partial<Parameters<typeof createCli>[0]> = {}) => {
    const s = { harness: 'claude' as const, ctx: ctx(), context: true, resumedFrom: null, ...over }
    return createCli(s, () => s.ctx, () => 100)
  }
  it('starts with the welcome, the loaded context and an empty input box', () => {
    const t = plain(open().start())
    expect(t).toContain('Welcome to Claude Code (simulated)')
    expect(t).toContain('Context: DEMO-0043 · Load tariff tables (current state loaded)')
    expect(t).toMatch(/│ > +│/)
    expect(t).toContain('/help for commands · Ctrl-C twice to quit')
    expect(t).not.toMatch(/for shortcuts|esc to interrupt|ctrl\+r/)
  })
  it('a fresh window has no ticket context; a resumed one shows the summary it was seeded with', () => {
    expect(plain(open({ context: false }).start())).toContain('Fresh window: no ticket context loaded')
    const r = plain(open({ resumedFrom: { label: 'DEMO-0043 · Codex', summary: 'Reviewed the plan.' } }).start())
    expect(r).toContain('Continued from DEMO-0043 · Codex')
    expect(r).toContain('Reviewed the plan.')
  })
  it('typing redraws the input; Enter answers with a scripted, clearly simulated reply', () => {
    const cli = open()
    cli.start()
    expect(plain(cli.feed('hi'))).toMatch(/│ > hi +│/)
    const out = plain(cli.feed('\r'))
    expect(out).toContain('> hi')
    expect(out).toContain('⏺ Bash(orch status)')
    expect(out).toContain('(Simulated) This mockup does not run Claude Code.')
  })
  it('/orch: commands are refused: agent work starts from Start agent, signed', () => {
    const cli = open()
    cli.feed('/orch:work DEMO-0043')
    expect(plain(cli.feed('\r'))).toContain('Start agent in the dashboard')
  })
  it('control and escape sequences typed or pasted are ignored; a multi-line paste keeps its text', () => {
    const cli = open()
    expect(cli.feed('\x1b]52;c;ZXZpbA==\x07')).toBe('')
    expect(cli.feed('\x9b31m')).toBe('')
    expect(plain(cli.feed('fix the\r\nseed\x07 loader'))).toMatch(/│ > fix the seed loader +│/)
  })
  it('/exit ends the session; Ctrl-C twice on an empty line too', () => {
    const a = open()
    a.feed('/exit')
    a.feed('\r')
    expect(a.exited()).toBe(true)
    expect(a.feed('x')).toBe('')
    const b = open({ harness: 'codex' })
    expect(plain(b.feed('\x03'))).toContain('Press Ctrl-C again to exit')
    b.feed('\x03')
    expect(b.exited()).toBe(true)
  })
  it('redraw repaints the conversation at the current width', () => {
    let cols = 100
    const s = { harness: 'claude' as const, ctx: ctx(), context: true, resumedFrom: null }
    const cli = createCli(s, () => s.ctx, () => cols)
    cli.feed('hello')
    cli.feed('\r')
    cols = 50
    const t = plain(cli.redraw())
    expect(t).toContain('> hello')
    expect(t.split('\r\n').find((l) => l.startsWith('╭'))!.length).toBe(49)
  })
})

describe('wrap', () => {
  it('wraps words to the width', () => expect(wrap('one two three four', 9)).toEqual(['one two', 'three', 'four']))
})
