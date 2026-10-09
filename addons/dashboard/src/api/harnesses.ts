// The terminal harnesses: what can run in a terminal session (a plain shell, Claude Code, Codex). One list, as data,
// so another harness is one more entry. Pure: the mock and the dashboard both import it.

export type HarnessId = 'shell' | 'claude' | 'codex'

export interface Harness {
  id: HarnessId
  /** What people read: "Claude Code". */
  label: string
  /** The tmux-style window name in the dock's status line: "claude". */
  window: string
  /** The command the session runs. `{ticket}` is the ticket key (only in `withContext`). */
  command: string
  /** The command when the session starts with the ticket's context; absent for harnesses without a context window. */
  withContext?: string
  /** orch agent ids that run on this harness (an agent mirror of `agent:claude-code` shows the Claude Code view). */
  agents: string[]
}

export const HARNESSES: Harness[] = [
  { id: 'shell', label: 'Shell', window: 'shell', command: '$SHELL -l', agents: [] },
  {
    id: 'claude',
    label: 'Claude Code',
    window: 'claude',
    command: 'claude',
    withContext: 'claude --append-system-prompt "$(orch show {ticket} --section current_state)"',
    agents: ['claude-code'],
  },
  {
    id: 'codex',
    label: 'Codex',
    window: 'codex',
    command: 'codex',
    withContext: 'codex --config instructions="$(orch show {ticket} --section current_state)"',
    agents: ['codex'],
  },
]

export const isHarness = (id: unknown): id is HarnessId => HARNESSES.some((h) => h.id === id)

export const harnessOf = (id: HarnessId): Harness => HARNESSES.find((h) => h.id === id) ?? HARNESSES[0]

/** The harness an orch agent runs on (`claude-code` → Claude Code); unknown agents show as a shell. */
export const harnessForAgent = (agent: string): HarnessId => HARNESSES.find((h) => h.agents.includes(agent))?.id ?? 'shell'

/** The exact command a new session runs (shown before it starts). */
export function harnessCommand(id: HarnessId, opts: { ticket?: string | null; context?: boolean } = {}): string {
  const h = harnessOf(id)
  return opts.context && opts.ticket && h.withContext ? h.withContext.replaceAll('{ticket}', opts.ticket) : h.command
}
