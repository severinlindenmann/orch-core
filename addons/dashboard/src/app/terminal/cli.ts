// Simulated agent CLIs (Claude Code, Codex) for the fake PTY. Pure: no xterm, no DOM, no clock. Nothing here runs a
// model: agent mirrors replay a scripted transcript built from the session's commands, and a person's own CLI session
// answers every prompt with a short scripted reply that says it is simulated.
// Every data-derived string (ticket title, branch, typed text, summaries, command output) goes through clean(); the
// only escape sequences written are the constant SGR colours and cursor moves defined in this file.

import type { HarnessId } from '@/api/harnesses'
import type { ShellCtx } from '@/api/terminals'
import { clean, runCommand, type Shell } from './fakePty'

export type CliHarness = Exclude<HarnessId, 'shell'>

/** What a CLI screen needs from a session. */
export interface CliSession {
  harness: CliHarness
  kind: 'person' | 'agent'
  status: 'running' | 'stopped'
  started: string
  ctx: ShellCtx
  transcript: string[]
  context: boolean
  summary: string | null
  resumedFrom: { label: string; summary: string | null } | null
}

/** A line the view keeps redrawing (the "working" spinner): `up` lines above the cursor. */
export interface LiveLine {
  up: number
  frame: (tick: number) => string
}

export interface Screen {
  text: string
  live?: LiveLine
}

// SGR only (no OSC, no hyperlinks). Colours map to the dashboard's terminal theme: green = success, red = danger,
// cyan = brand teal, dim = faint text. Never orange.
const ESC = '\x1b['
const dim = (s: string) => `${ESC}2m${s}${ESC}22m`
const bold = (s: string) => `${ESC}1m${s}${ESC}22m`
const green = (s: string) => `${ESC}32m${s}${ESC}39m`
const red = (s: string) => `${ESC}31m${s}${ESC}39m`
const teal = (s: string) => `${ESC}36m${s}${ESC}39m`
const NL = '\r\n'
const CLEAR = '\x1b[2J\x1b[H'

const chars = (s: string) => Array.from(s)
const len = (s: string) => chars(s).length
/** Cut to `n` characters (with … when cut) and pad to exactly `n`. */
const fit = (s: string, n: number) => {
  const c = chars(s)
  const cut = c.length > n ? c.slice(0, Math.max(0, n - 1)).join('') + '…' : s
  return cut + ' '.repeat(Math.max(0, n - len(cut)))
}
/** Word-wrap plain text to `n` columns. */
export function wrap(s: string, n: number): string[] {
  const out: string[] = []
  let line = ''
  for (const word of s.split(/\s+/).filter(Boolean)) {
    if (line && len(line) + 1 + len(word) > n) {
      out.push(line)
      line = ''
    }
    line = line ? `${line} ${word}` : word.length > n ? chars(word).slice(0, n).join('') : word
  }
  if (line) out.push(line)
  return out
}

export const boxWidth = (cols: number) => Math.max(30, Math.min(cols - 1, 76))

/** A rounded box of `w` columns around `lines` (each cut to fit). */
function box(lines: string[], w: number, paint: (s: string) => string = (s) => s): string[] {
  const inner = w - 4
  return [dim(`╭${'─'.repeat(w - 2)}╮`), ...lines.map((l) => `${dim('│')} ${paint(fit(l, inner))} ${dim('│')}`), dim(`╰${'─'.repeat(w - 2)}╯`)]
}

const SPIN = ['·', '✢', '✳', '∗', '✻', '✽', '✻', '∗', '✳', '✢']
const elapsed = (sec: number) => (sec >= 3600 ? `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m` : sec >= 60 ? `${Math.floor(sec / 60)}m ${sec % 60}s` : `${sec}s`)

function welcome(h: CliHarness, c: ShellCtx, w: number): string[] {
  if (h === 'claude')
    return box([`✻ Welcome to Claude Code (simulated)`, '', '  /help for help, /status for your current setup', '', `  cwd: ${clean(c.cwd)} (${clean(c.branch)})`], w)
  return box([`>_ OpenAI Codex (simulated)`, '', `model:     gpt-5-codex`, `directory: ${clean(c.cwd)} (${clean(c.branch)})`], w)
}

/** One line under the welcome: what the session was started with. */
function contextLines(s: Pick<CliSession, 'ctx' | 'context' | 'resumedFrom'>, w: number): string[] {
  const t = s.ctx.ticket
  const out: string[] = []
  if (s.resumedFrom) {
    out.push(dim(` ⎿  Resumed from ${clean(s.resumedFrom.label)}`))
    for (const l of wrap(s.resumedFrom.summary ? clean(s.resumedFrom.summary) : 'No summary was recorded.', w - 6)) out.push(dim(`    ${l}`))
  } else if (s.context && t) out.push(dim(` ⎿  Context: ${clean(t.key)} · ${clean(t.title)} (current state loaded)`))
  else out.push(dim(' ⎿  Fresh window: no ticket context loaded'))
  return out
}

/** A narration line for what the agent is about to run. */
function narrate(cmd: string): string {
  if (cmd.startsWith('orch status')) return 'Checking where the ticket stands.'
  if (cmd.startsWith('orch task next')) return 'Picking up the next task.'
  if (cmd.startsWith('orch show')) return 'Reading the ticket section.'
  if (cmd.startsWith('orch approve')) return 'The plan is ready; trying to approve its gate.'
  if (cmd.startsWith('git')) return 'Looking at the branch.'
  return 'Looking around the worktree.'
}

/** A tool call and its (cut) result, in the harness's own style. */
function toolCall(h: CliHarness, cmd: string, c: ShellCtx): string[] {
  const lines = runCommand(cmd, c).lines
  const failed = lines.some((l) => l.startsWith('err '))
  const shown = lines.slice(0, 3)
  const more = lines.length - shown.length
  if (h === 'claude') {
    const mark = failed ? red('⏺') : green('⏺')
    return [
      `${mark} ${bold('Bash')}(${clean(cmd)})`,
      ...shown.map((l, i) => `${dim(i === 0 ? '  ⎿  ' : '     ')}${l}`),
      ...(lines.length === 0 ? [dim('  ⎿  (no output)')] : []),
      ...(more > 0 ? [dim(`     … +${more} lines (ctrl+r to expand)`)] : []),
      '',
    ]
  }
  return [
    `${failed ? red('•') : green('•')} ${bold('Ran')} ${clean(cmd)}`,
    ...shown.map((l, i) => `${dim(i === 0 ? '  └ ' : '    ')}${l}`),
    ...(more > 0 ? [dim(`    … +${more} lines`)] : []),
    '',
  ]
}

const say = (h: CliHarness, text: string, w: number) => wrap(text, w - 3).map((l, i) => (i === 0 ? `${h === 'claude' ? '⏺' : '•'} ${l}` : `  ${l}`))

/** The prompt the agent session was started with (its orch slash command). */
const agentPrompt = (c: ShellCtx) => (c.ticket ? `/orch:work ${clean(c.ticket.key)}` : '/orch:next')

/** An agent mirror or an ended session, replayed as the CLI would have shown it. */
export function cliScreen(s: CliSession, cols: number): Screen {
  const w = boxWidth(cols)
  const h = s.harness
  const out: string[] = [...welcome(h, s.ctx, w), '']
  if (s.kind === 'person') out.push(...contextLines(s, w), '')
  else out.push(`${h === 'claude' ? '>' : '›'} ${agentPrompt(s.ctx)}`, '')
  let waiting = false
  for (const cmd of s.transcript) {
    out.push(...say(h, narrate(cmd), w), '')
    out.push(...toolCall(h, cmd, s.ctx))
    waiting = cmd.startsWith('orch approve')
    if (waiting) out.push(...say(h, 'Approving is human-only. Waiting for a person to approve the plan in orch.', w), '')
  }
  if (s.status === 'stopped') {
    out.push(...say(h, s.summary ? `Session ended. Summary: ${clean(s.summary)}` : 'Session ended. No summary was recorded.', w), '')
    return { text: out.join(NL) + NL + '[process completed]' + NL }
  }
  // Running mirror: a working line above an inert input, as the CLI shows while it works.
  const verb = waiting ? 'Waiting for approval' : 'Working'
  const base = Math.max(0, Math.round((Date.parse(s.ctx.now) - Date.parse(s.started)) / 1000)) || 0
  const frame = (tick: number) =>
    h === 'claude'
      ? `${teal(SPIN[tick % SPIN.length])} ${teal(`${verb}…`)} ${dim(`(esc to interrupt · ${elapsed(base + Math.floor(tick / 5))})`)}`
      : `${teal(tick % 2 ? '◦' : '•')} ${bold(verb)} ${dim(`(${elapsed(base + Math.floor(tick / 5))} • esc to interrupt)`)}`
  const input = h === 'claude' ? [...box(['> '], w, dim), dim('  view only · agent session')] : [dim('▌ '), dim('  view only · agent session')]
  out.push(frame(0), '', ...input)
  return { text: out.join(NL), live: { up: input.length + 1, frame } }
}

// ------------------------------------------------------------------------------------------------ interactive CLI

/** The input area at the bottom: its lines, the row holding the input, and the cursor column (1-based). */
function inputFrame(h: CliHarness, buf: string, hint: string, w: number) {
  if (h === 'claude') {
    const room = w - 6
    const shown = len(buf) > room ? '…' + chars(buf).slice(-(room - 1)).join('') : buf
    return { lines: [dim(`╭${'─'.repeat(w - 2)}╮`), `${dim('│')} > ${fit(shown, w - 6)} ${dim('│')}`, dim(`╰${'─'.repeat(w - 2)}╯`), dim(`  ${hint}`)], row: 1, col: 5 + len(shown) }
  }
  const room = w - 3
  const shown = len(buf) > room ? '…' + chars(buf).slice(-(room - 1)).join('') : buf
  return { lines: [`${teal('▌')} ${shown}`, dim(`  ${hint}`)], row: 0, col: 3 + len(shown) }
}
/** Draws the frame from the current line, leaving the cursor in the input. */
const drawFrame = (f: ReturnType<typeof inputFrame>) => {
  const up = f.lines.length - 1 - f.row
  return f.lines.join(NL) + (up > 0 ? `${ESC}${up}A` : '') + `${ESC}${f.col}G`
}
const HINT = { claude: '? for shortcuts', codex: '⏎ send   ⌃C quit   /status' } as const

/** Prompts both CLIs understand. */
const COMMANDS = ['/help', '/status', '/clear', '/exit']

function reply(h: CliHarness, text: string, c: ShellCtx, context: boolean, w: number): string[] {
  const t = context ? c.ticket : null
  const out = [dim(`${h === 'claude' ? '>' : '›'} ${text}`), '']
  if (text.startsWith('/orch:'))
    return [...out, ...say(h, 'Agent work on a ticket starts from Start agent in the dashboard, which you sign. This simulated CLI does not claim tickets.', w), '']
  out.push(...say(h, t ? `Looking at ${clean(t.key)} first.` : 'Looking around the worktree first.', w), '')
  if (t) out.push(...toolCall(h, 'orch status', c))
  out.push(...toolCall(h, 'ls', c))
  out.push(...say(h, `(Simulated) This mockup does not run ${h === 'claude' ? 'Claude Code' : 'Codex'}. A real session would answer "${text.length > 60 ? text.slice(0, 59) + '…' : text}" here.`, w), '')
  return out
}

function slash(h: CliHarness, cmd: string, c: ShellCtx, context: boolean): string[] {
  const out = [dim(`${h === 'claude' ? '>' : '›'} ${cmd}`), '']
  if (cmd === '/help') return [...out, ...COMMANDS.map((x) => `  ${x}`), dim('  Anything else is a prompt (answered by a script in this mockup).'), '']
  const t = c.ticket
  return [
    ...out,
    `  harness   ${h === 'claude' ? 'Claude Code' : 'Codex'} (simulated)`,
    `  cwd       ${clean(c.cwd)}`,
    `  branch    ${clean(c.branch)}`,
    `  context   ${context && t ? `${clean(t.key)} · ${clean(t.title)}` : 'none (fresh window)'}`,
    `  grant     ${c.grant ? `${clean(c.grant.id)} · ${clean(c.grant.scope)}` : 'none'}`,
    '',
  ]
}

/**
 * A person's own Claude Code or Codex session: the CLI look, an input line that is redrawn as you type, and a
 * scripted reply per prompt. Same contract as createShell (feed key data, get back what to write). Only ever
 * created for an interactive session (the owner of a running session, member or above); mirrors never get one.
 */
export function createCli(s: Omit<CliSession, 'kind' | 'status' | 'transcript' | 'summary' | 'started'>, ctx: () => ShellCtx, cols: () => number): Shell & { start(): string; redraw(): string } {
  const h = s.harness
  let buf = ''
  let done = false
  let armed = false // Ctrl-C once on an empty line: the next one quits
  let hist: string[] = []
  let shown: string[] = [] // prompts on screen since the last /clear (redrawn at a new width)
  const w = () => boxWidth(cols())
  const frame = (hint: string = HINT[h]) => inputFrame(h, buf, hint, w())
  const top = () => {
    const f = frame()
    return (f.row > 0 ? `${ESC}${f.row}A` : '') + '\r' + `${ESC}J`
  }
  const head = () => [...welcome(h, ctx(), w()), '', ...contextLines({ ...s, ctx: ctx() }, w()), ''].join(NL) + NL
  const answer = (text: string) => (text === '/help' || text === '/status' ? slash(h, text, ctx(), s.context) : reply(h, text, ctx(), s.context, w()))
  const start = () => head() + drawFrame(frame())
  /** The whole screen again at the current width (after a resize): boxes are drawn for the new column count. */
  const redraw = () => CLEAR + head() + shown.map((t) => answer(t).join(NL) + NL).join('') + drawFrame(frame(armed ? 'Press Ctrl-C again to exit' : HINT[h]))
  const redrawInput = () => {
    const f = frame()
    return `\r${ESC}2K${f.lines[f.row]}${ESC}${f.col}G`
  }
  const feed = (data: string): string => {
    if (done) return ''
    if (data === '\x03') {
      if (buf) {
        buf = ''
        return redrawInput()
      }
      if (armed) {
        done = true
        return top() + dim('Bye.') + NL
      }
      armed = true
      const clear = top()
      return clear + drawFrame(frame('Press Ctrl-C again to exit'))
    }
    if (armed) {
      armed = false
      const clear = top()
      if (data === '\x1b') return clear + drawFrame(frame())
      const rest = feed(data)
      return clear + drawFrame(frame()) + rest
    }
    if (data === '\x1b') {
      if (!buf) return ''
      buf = ''
      return redrawInput()
    }
    if (data === '\x7f') {
      if (!buf) return ''
      buf = chars(buf).slice(0, -1).join('')
      return redrawInput()
    }
    if (data === '\r') {
      const text = clean(buf).trim()
      const clear = top()
      buf = ''
      if (!text) return clear + drawFrame(frame())
      hist = [...hist, text]
      if (text === '/exit' || text === '/quit' || text === 'exit') {
        done = true
        return clear + dim(`${h === 'claude' ? '>' : '›'} ${text}`) + NL + dim('Bye.') + NL
      }
      if (text === '/clear') {
        shown = []
        return CLEAR + start()
      }
      shown = [...shown, text]
      return clear + answer(text).join(NL) + NL + drawFrame(frame())
    }
    // Printable text (a paste may carry several characters); control and escape sequences are ignored.
    if (/^[^\x00-\x1f\x7f-\x9f]+$/.test(data)) {
      buf += data
      return redrawInput()
    }
    return ''
  }
  return { start, redraw, prompt: start, feed, history: () => hist, exited: () => done }
}
