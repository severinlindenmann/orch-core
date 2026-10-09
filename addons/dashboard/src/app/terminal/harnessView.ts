// What a session looks like in the terminal, by harness: a shell prompt or an agent CLI (Claude Code, Codex).
// Pure: the terminal view asks for the first screen and, for an interactive session, the line editor to feed.

import type { TerminalSessionView } from '@/api/terminals'
import { cliScreen, createCli, type Screen } from './cli'
import { clean, createShell, replay, type Shell } from './fakePty'

export type { LiveLine, Screen } from './cli'

/** The replayed screen of a session nobody types into (an agent mirror, an ended session, a viewer's view). */
export function sessionScreen(s: TerminalSessionView, cols: number): Screen {
  if (s.harness === 'shell') return { text: replay(s.ctx, s.transcript) + (s.status === 'stopped' ? '[process completed]\r\n' : '') }
  return cliScreen({ ...s, harness: s.harness }, cols)
}

/** The line editor of an interactive session and its first screen. `session` is read live (ctx at command time). */
export function openSession(session: () => TerminalSessionView, cols: () => number): Shell & { start(): string; redraw?(): string } {
  const s = session()
  if (s.harness === 'shell') {
    const shell = createShell(() => session().ctx)
    const resumed = s.resumedFrom ? [`# resumed from ${clean(s.resumedFrom.label)}`, ...(s.resumedFrom.summary ? [`# ${clean(s.resumedFrom.summary)}`] : [])].map((l) => l + '\r\n').join('') : ''
    return { ...shell, start: () => resumed + shell.prompt() }
  }
  return createCli({ harness: s.harness, ctx: s.ctx, context: s.context, resumedFrom: s.resumedFrom }, () => session().ctx, cols)
}
