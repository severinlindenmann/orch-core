// The terminal harnesses: what can run in a terminal session (a plain shell, Claude Code, Codex). One list, as data,
// so another harness is one more entry. The UI branches on `capabilities` and `view`, never on a harness id.
// Pure: the mock and the dashboard both import it.

export type HarnessId = 'shell' | 'claude' | 'codex'

export interface HarnessCapabilities {
  /** The owner can type into their own session of it. */
  interactive: boolean
  /** It can start with the ticket's current-state summary loaded. */
  contextInjection: boolean
  /** The harness itself can resume a conversation (the dock still continues from the summary). */
  nativeResume: boolean
  /** An ended session leaves a transcript to read. */
  transcript: boolean
}

export interface Harness {
  id: HarnessId
  /** What people read: "Claude Code". */
  label: string
  /** Short name in session names: "Claude" ("Claude · Yours"). */
  short: string
  /** How the terminal draws it: a shell prompt, or one of the simulated agent CLIs (src/app/terminal/cli.ts). */
  view: 'shell' | 'claude-cli' | 'codex-cli'
  capabilities: HarnessCapabilities
  /** The command the session runs. */
  command: string
  /** The command with the ticket's current-state summary loaded (`{ticket}` is the key); only with contextInjection. */
  withContext?: string
  /** orch agent ids that run on this harness (an agent session of `agent:claude-code` runs Claude Code). */
  agents: string[]
}

export const HARNESSES: Harness[] = [
  {
    id: 'shell',
    label: 'Shell',
    short: 'Shell',
    view: 'shell',
    capabilities: { interactive: true, contextInjection: false, nativeResume: false, transcript: true },
    command: '$SHELL -l',
    agents: [],
  },
  {
    id: 'claude',
    label: 'Claude Code',
    short: 'Claude',
    view: 'claude-cli',
    capabilities: { interactive: true, contextInjection: true, nativeResume: true, transcript: true },
    command: 'claude',
    withContext: 'claude --append-system-prompt "$(orch show {ticket} --section current_state)"',
    agents: ['claude-code'],
  },
  {
    id: 'codex',
    label: 'Codex',
    short: 'Codex',
    view: 'codex-cli',
    capabilities: { interactive: true, contextInjection: true, nativeResume: true, transcript: true },
    command: 'codex',
    withContext: 'codex --config instructions="$(orch show {ticket} --section current_state)"',
    agents: ['codex'],
  },
]

export const isHarness = (id: unknown): id is HarnessId => HARNESSES.some((h) => h.id === id)

/** The harness, or undefined for an id this dashboard does not know (shown as "Unsupported harness", never as a shell). */
export const findHarness = (id: string): Harness | undefined => HARNESSES.find((h) => h.id === id)

/** The harness an orch agent runs on (`claude-code` → claude); an unknown agent keeps its own id (unsupported). */
export const harnessForAgent = (agent: string): string => HARNESSES.find((h) => h.agents.includes(agent))?.id ?? agent

/** The exact command a new session runs (shown before it starts). */
export function harnessCommand(id: string, opts: { ticket?: string | null; context?: boolean } = {}): string {
  const h = findHarness(id)
  if (!h) return id
  return opts.context && opts.ticket && h.capabilities.contextInjection && h.withContext ? h.withContext.replaceAll('{ticket}', opts.ticket) : h.command
}

/** What a session is called in the dock: "Claude · Agent", "Claude · Yours", "Codex · Review", "Shell". */
export function sessionName(s: { harness: string; kind: 'person' | 'agent'; purpose?: string | null }): string {
  const h = findHarness(s.harness)
  if (!h) return `Unsupported harness ${s.harness}`
  if (s.kind === 'agent') return `${h.short} · ${s.purpose ?? 'Agent'}`
  return h.view === 'shell' ? 'Shell' : `${h.short} · Yours`
}
