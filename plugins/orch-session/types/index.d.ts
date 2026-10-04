// The roles of orch-core's design system (DESIGN.md): `you` (pink) is the human's move and nothing else.
export type Role = 'you' | 'info' | 'warn' | 'err' | 'ok' | 'neu'

// Whose move it is, as orch-core says (`move` in `orch show --json`): who, what (orch's `kind`), its label, ref and
// why; `role` is the design-system role drawn for it. `what: 'outdated'` is an orch without the field.
export type Move = {
  who: 'you' | 'agent' | 'nobody'
  what: string
  label: string
  role: Role
  ref: string | null
  why: string | null
}

export type Task = { id: string; state: string; text: string; why: string | null; owner: string }

export type Question = {
  id: string
  text: string
  options: { key: string; label: string }[]
  recommended: string | null
  // `orch answer …` for the human to run in their own terminal; the plugin never runs it
  command: string
}

export type Ticket = {
  id: string
  title: string
  status: string
  epic: string | null
  move: Move
  // the one thing that happens next, in words
  detail: string
  gates: { requirements: string; plan: string }
  verdict: string | null
  tasks: Task[]
  doing: string | null
  closed: number
  total: number
  question: Question | null
  // unanswered non-blocking questions: the agent went ahead on its recommendation
  confirm: string[]
  pr: { label: string; url: string } | null
}

// Why the last refresh kept the previous state (or shows nothing): never a path, a message or stderr.
export type Problem = 'no-workspace' | 'missing' | 'timeout' | 'failed' | 'unreadable' | 'outdated'

declare module 'claude-code' {
  interface PluginState {
    'orch-session': { tickets: Ticket[] | null; problem: Problem | null }
  }
}
