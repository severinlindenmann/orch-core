// What a session looks like in the terminal, by its harness's `view`: a shell prompt, an agent CLI (Claude Code,
// Codex), or — for a harness id this dashboard does not know — a plain read-only transcript. Pure, plus one helper
// that drives a mounted xterm (first screen, live line, redraw on resize, input) so TerminalView stays generic.

import { findHarness } from '@/api/harnesses'
import type { TerminalSessionView } from '@/api/terminals'
import { cliScreen, createCli, type CliHarness, type LiveLine, type Screen } from './cli'
import { clean, createShell, promptOf, replay, runCommand, type Shell } from './fakePty'

export type { LiveLine, Screen } from './cli'

const HIDE_CURSOR = '\x1b[?25l'

const cliOf = (s: Pick<TerminalSessionView, 'harness'>): CliHarness | null => {
  const v = findHarness(s.harness)?.view
  return v === 'claude-cli' ? 'claude' : v === 'codex-cli' ? 'codex' : null
}

/** An unknown harness: say so, and show its commands as inert text. Never drawn as a working shell. */
function unsupported(s: TerminalSessionView): Screen {
  const lines = [`Unsupported harness ${clean(s.harness)} — this dashboard can only show its transcript.`, '']
  for (const cmd of s.transcript) lines.push(`$ ${clean(cmd)}`, ...runCommand(cmd, s.ctx).lines)
  return { text: lines.join('\r\n') + '\r\n' + (s.status === 'stopped' ? '[process completed]\r\n' : '') }
}

/** The replayed screen of a session nobody types into (an agent session, an ended one, a viewer's view). */
export function sessionScreen(s: TerminalSessionView, cols: number): Screen {
  if (!findHarness(s.harness)) return unsupported(s)
  const cli = cliOf(s)
  if (!cli) return { text: replay(s.ctx, s.transcript) + (s.status === 'stopped' ? '[process completed]\r\n' : '') }
  return cliScreen({ ...s, harness: cli }, cols)
}

/** The line editor of an interactive session and its first screen. `session` is read live (ctx at command time). */
export function openSession(session: () => TerminalSessionView, cols: () => number): Shell & { start(): string; redraw?(): string } {
  const s = session()
  const cli = cliOf(s)
  if (!cli) {
    const shell = createShell(() => session().ctx)
    const resumed = s.resumedFrom ? [`# continued from ${clean(s.resumedFrom.label)}`, ...(s.resumedFrom.summary ? [`# ${clean(s.resumedFrom.summary)}`] : [])].map((l) => l + '\r\n').join('') : ''
    return { ...shell, start: () => resumed + promptOf(session().ctx) }
  }
  return createCli({ harness: cli, ctx: s.ctx, context: s.context, resumedFrom: s.resumedFrom }, () => session().ctx, cols)
}

/** The part of a mounted terminal that depends on the harness, driven by TerminalView's mount effect. */
export interface ScreenDriver {
  /** The text to write first (unless TerminalView restores a cached screen). */
  first(): string
  /** Key data from xterm; returns what to write. Only for interactive sessions. */
  feed?(data: string): string
  exited(): boolean
  /** Called when the column count changed: the full screen to write after a reset, or null to let xterm rewrap. */
  resized(): string | null
  /** Starts the live line (spinner) if the screen has one; returns a stop function. */
  live(write: (text: string) => void): () => void
}

/**
 * The harness behaviour of one mounted terminal. Agent CLIs draw boxes for their width, so after a resize they are
 * drawn again; a running agent CLI keeps its working line ticking (not under reduced motion).
 */
export function screenDriver(session: () => TerminalSessionView, interactive: boolean, cols: () => number): ScreenDriver {
  const shell = interactive ? openSession(session, cols) : undefined
  let screen: Screen | undefined
  let stop = () => {}
  let restart: ((line: LiveLine | undefined) => void) | undefined
  return {
    first: () => {
      if (shell) return shell.start()
      screen = sessionScreen(session(), cols())
      return HIDE_CURSOR + screen.text // nobody types here: no cursor that suggests otherwise
    },
    feed: shell ? (d) => shell.feed(d) : undefined,
    exited: () => !!shell?.exited(),
    resized: () => {
      if (!cliOf(session()) || shell?.exited()) return null
      if (shell) return shell.redraw ? shell.redraw() : null
      screen = sessionScreen(session(), cols())
      restart?.(screen.live)
      return HIDE_CURSOR + screen.text
    },
    live: (write) => {
      let timer: ReturnType<typeof setInterval> | undefined
      restart = (line) => {
        clearInterval(timer)
        if (!line || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
        let tick = 0
        const up = line.up > 0 ? `\x1b[${line.up}A` : ''
        timer = setInterval(() => write(`\x1b7${up}\r\x1b[2K${line.frame(++tick)}\x1b8`), 200)
      }
      restart(screen?.live)
      stop = () => clearInterval(timer)
      return () => stop()
    },
  }
}
