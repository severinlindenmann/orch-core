// A tiny line-based shell for the terminals addon. Pure: no xterm, no DOM, no clock. The terminal view feeds it key
// data and writes back whatever it returns; the mock supplies the live data (`ShellCtx`) at command time.

import type { ShellCtx } from '@/api/terminals'

export type { ShellCtx }

export interface CommandResult {
  lines: string[]
  clear?: boolean
  exit?: boolean
}

const HELP = ['orch status', 'orch show <key> --section <name>', 'orch task next', 'git status', 'git log --oneline -5', 'ls', 'pwd', 'clear', 'help', 'exit']
const HUMAN_ONLY = 'err human_only approve · retry:false · next: orch ask or orch wait'
const CLEAR = '\x1b[2J\x1b[H'

const hhmm = (iso: string) => `${iso.slice(11, 16)} UTC`
const left = (until: string, now: string) => {
  const min = Math.max(0, Math.round((Date.parse(until) - Date.parse(now)) / 60000))
  return min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, '0')} min left` : `${min} min left`
}
const label = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ')

/**
 * The only door for data-derived text into the terminal. Strips ESC and every C0/C1 control character (so no escape,
 * OSC, CSI, hyperlink or clipboard sequence survives), leaving printable text. Newlines are handled before this by
 * splitting into lines; each line is then written with \r\n.
 */
export const clean = (s: unknown): string => String(s ?? '').replace(/[\u0000-\u001f\u007f-\u009f]/g, '')

export const promptOf = (c: ShellCtx) => clean(`${c.user}@acme ${c.cwd} (${c.branch}) $ `)

/** Run one command line against the live context; every output line is cleaned. */
export function runCommand(line: string, c: ShellCtx): CommandResult {
  const r = run(line, c)
  return { ...r, lines: r.lines.flatMap((l) => l.split(/\r\n|\r|\n/)).map(clean) }
}

function run(line: string, c: ShellCtx): CommandResult {
  const argv = line.trim().split(/\s+/).filter(Boolean)
  if (!argv.length) return { lines: [] }
  const [cmd, sub, ...rest] = argv
  const t = c.ticket
  if (cmd === 'orch') {
    if (sub === 'status')
      return {
        lines: t
          ? [
              `${t.key} · ${t.title} · ${t.status}`,
              `move       ${t.move.who} · ${t.move.why}`,
              `gates      ${t.gates.map((g) => `${g.name} ${g.state}`).join(' · ')}`,
              `questions  ${t.questions.open} open of ${t.questions.total}`,
              c.claim ? `claim      ${c.claim.agent} ${c.claim.session} · for ${c.claim.for} · expires ${hhmm(c.claim.expires)}` : 'claim      none',
              `tasks      ${t.tasks.done}/${t.tasks.total} done${t.tasks.doing ? ` · doing ${t.tasks.doing}` : ''}${t.next_task ? ` · next ${t.next_task.id}` : ''}`,
              `cursor     ${c.cursor}`,
              c.grant ? `grant      ${c.grant.id} · ${c.grant.scope} · until ${hhmm(c.grant.until)} (${left(c.grant.until, c.now)})` : 'grant      none · run orch grant',
            ]
          : ['orch · no ticket in this shell'],
      }
    if (sub === 'show') {
      const key = rest[0]
      const section = rest[rest.indexOf('--section') + 1]
      if (!t || key !== t.key) return { lines: [`err not_found · no ticket ${key ?? ''} in this shell`] }
      if (section !== 'current_state') return { lines: [`err bad_section · ${section ?? 'missing'} · next: orch show ${t.key} --section current_state`] }
      return { lines: [`${t.key} · ${label(section)}`, t.current_state] }
    }
    if (sub === 'task' && rest[0] === 'next') return { lines: [t?.next_task ? `next ${t.next_task.id} · ${t.next_task.text}` : 'no open tasks'] }
    if (sub === 'approve') return { lines: [c.owner === 'agent' ? HUMAN_ONLY : 'approve needs Touch ID: use the dashboard'] }
    return { lines: [`err unknown_command · orch ${sub ?? ''}`.trimEnd() + ' · next: help'] }
  }
  if (cmd === 'git' && sub === 'status') return { lines: [`On branch ${c.branch}`, 'Your branch is up to date with origin.', 'nothing to commit, working tree clean'] }
  if (cmd === 'git' && sub === 'log')
    return {
      lines: ['4be1f07 seeds: tariff_ch_2026 loader', '9a03c2d dbt: add price range test', 'c7e5b18 seeds: valid_from on every row', '1d2f9e4 reconcile: tolerance from settings', 'e60a7bb init: energy workspace'],
    }
  if (cmd === 'ls') return { lines: ['dbt_project.yml  models  seeds  tests  README.md'] }
  if (cmd === 'pwd') return { lines: [`/Users/${c.user === 'claude' ? 'claude' : 'severin'}${c.cwd.replace(/^~/, '')}`] }
  if (cmd === 'clear') return { lines: [], clear: true }
  if (cmd === 'help') return { lines: [`commands: ${HELP.join(', ')}`] }
  if (cmd === 'exit') return { lines: ['logout'], exit: true }
  return { lines: [`zsh: command not found: ${cmd}`] }
}

/** Prompt, command and output for each command, as one string for the terminal (the agent mirror). */
export function replay(c: ShellCtx, commands: string[]): string {
  let out = ''
  for (const cmd of commands) {
    out += promptOf(c) + clean(cmd) + '\r\n'
    const r = runCommand(cmd, c)
    for (const l of r.lines) out += l + '\r\n'
    if (r.exit) return out
  }
  return out + promptOf(c)
}

export interface Shell {
  prompt(): string
  /** Key data from the terminal; returns what to write back. After `exit` it returns ''. */
  feed(data: string): string
  history(): string[]
  exited(): boolean
}

/** The line editor: typing, Backspace, Enter, arrow-up/down history, Ctrl-C. `ctx` is read at command time. */
export function createShell(ctx: () => ShellCtx): Shell {
  let buf = ''
  let hist: string[] = []
  let at = -1 // -1: editing a new line; otherwise an index into hist
  let done = false
  const erase = () => '\b \b'.repeat(buf.length)
  const recall = () => {
    const out = erase()
    buf = at < 0 ? '' : hist[at]
    return out + buf
  }
  const feed = (data: string): string => {
    if (done) return ''
    if (data === '\x1b[A') {
      if (!hist.length || at === 0) return ''
      at = at < 0 ? hist.length - 1 : at - 1
      return recall()
    }
    if (data === '\x1b[B') {
      if (at < 0) return ''
      at = at + 1 >= hist.length ? -1 : at + 1
      return recall()
    }
    if (data === '\x03') {
      buf = ''
      at = -1
      return '^C\r\n' + promptOf(ctx())
    }
    if (data === '\x7f') {
      if (!buf) return ''
      buf = buf.slice(0, -1)
      return '\b \b'
    }
    if (data === '\r') {
      const line = buf
      buf = ''
      at = -1
      if (line.trim()) hist = [...hist, line]
      const c = ctx()
      const r = runCommand(line, c)
      if (r.clear) return CLEAR + promptOf(c)
      let out = '\r\n' + r.lines.map((l) => l + '\r\n').join('')
      if (r.exit) {
        done = true
        return out
      }
      out += promptOf(c)
      return out
    }
    // Printable text (a paste may carry several characters); control and escape sequences are ignored.
    if (/^[^\x00-\x1f\x7f-\x9f]+$/.test(data)) {
      buf += data
      return data
    }
    return ''
  }
  return { prompt: () => promptOf(ctx()), feed, history: () => hist, exited: () => done }
}
