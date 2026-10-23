// Start agent on a real host (M7b, contract v1.6 sections 12.3 and 12.4, H11c). The routes are the dashboard server's
// typed ones; nothing here names a host op. Every string from the host (display, path, message, warning) is untrusted:
// the page shows it through visible.tsx. The runner grant's secret never comes here: the click signer hands it to the
// runner itself, and only `deposit`, `deposit_id` and the grant id reach this page.

import { queryOptions } from '@tanstack/react-query'
import type { DepositOutcome, DepositReason } from './click'
import { call, type Run, type RunnerInfo } from './runs'

export type Harness = 'claude-code' | 'codex'
export type AgentMode = 'refine' | 'work' | 'fix' | 'continue' | 'context'
export const AGENT_MODES: readonly AgentMode[] = ['refine', 'work', 'fix', 'continue', 'context']
export const HARNESSES: readonly { id: Harness; name: string }[] = [
  { id: 'claude-code', name: 'Claude Code' },
  { id: 'codex', name: 'Codex' },
]

export interface AgentArgs {
  ticket?: string
  harness: Harness
  mode: AgentMode
  model?: string
  subagent_model?: string
  label?: string
  worktree: boolean
}

export interface RunnerGrant {
  id: string
  until: string
  scope: 'all' | 'workable'
  /** `whoami.runner.grant` only: the host's own launch rule (15-minute floor, member scope, revoked grant, role change). */
  startable?: boolean
  /** When the 15-minute floor is reached and the grant stops being startable; null when it is not startable now. */
  startable_until?: string | null
}

export interface AgentBlocked {
  code: string | null
  message: string | null
  detail: { reason?: 'none' | 'expiring' | 'scope' | 'revoked'; missing?: 'addon' | 'machine' | 'terminals' }
}

export interface AgentPreview {
  spec: string | null
  ticket: string | null
  display: string | null
  /** Model routing: the model this start would use (null: the agent's own default) and who chose it. */
  model?: string | null
  model_source?: 'chosen' | 'workspace' | 'default' | null
  display_hidden: boolean
  cwd: { kind: 'workspace' | 'repo' | 'worktree' | 'workdir' | null; repo?: string; ticket?: string }
  cwd_path: string | null
  /** For a work folder: whether it already exists and whether it holds anything (an unattended start needs it new or empty). */
  workdir?: { exists: boolean; empty: boolean }
  cwd_path_hidden: boolean
  will_create_worktree: boolean
  login_shell: boolean
  env: string[]
  grant: RunnerGrant | null
  blocked: AgentBlocked | null
  warnings: string[]
}

export interface Runner extends RunnerInfo {
  grant: RunnerGrant | null
}

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

export const agentsApi = {
  preview: (ws: string, args: AgentArgs) => call<AgentPreview>('POST', `${w(ws)}/agents/preview`, args),
  /** Exactly the preview's values plus its `spec`, with the dialog's one idem (a retry returns the same run). */
  start: (ws: string, args: AgentArgs, spec: string, idem: string) => call<{ run: Run }>('POST', `${w(ws)}/agents/start`, { ...args, spec, idem }),
  runner: (ws: string) => call<{ runner: Runner | null }>('GET', `${w(ws)}/runner`),
  depositStatus: (ws: string, depositId: string) =>
    call<{ deposit: DepositOutcome | 'none'; reason?: DepositReason }>('GET', `${w(ws)}/runner/deposits/${encodeURIComponent(depositId)}`),
  forget: (ws: string, grant: string, idem: string) => call<{ forgotten: boolean }>('POST', `${w(ws)}/runner/forget`, { grant, idem }),
  /** Which risk notices this person already read (kept by the dashboard server, per person and workspace). */
  riskNotices: (ws: string) => call<{ acknowledged: { start_agent?: boolean; login_shell?: boolean; grant?: boolean } }>('GET', `${w(ws)}/me/risk_notices`),
  acknowledge: (ws: string, notice: 'start_agent' | 'login_shell' | 'grant') => call<{ acknowledged: { start_agent?: boolean; login_shell?: boolean; grant?: boolean } }>('POST', `${w(ws)}/me/risk_notices`, { notice }),
}

export const runnerQuery = (ws: string) => queryOptions({ queryKey: ['runner', ws], queryFn: () => agentsApi.runner(ws) })

// -- approved texts (owner / orch-52 answers of 15 Oct 2026) ----------------------------------------------------------

/** D-P2-27, the owner's risk sentence for the runner grant, verbatim (also core's DEPOSIT_RISK, in the View's lines). */
export { DEPOSIT_RISK } from './click'

/** The approved line when `agent.start` creates the worktree first. */
export const worktreeLine = (key: string, repo: string, path: string) => `A worktree for ${key} in ${repo} will be created at ${path}.`
/** The approved words for `cwd.kind` "repo". */
export const SHARED_COPY = "the repository's shared working copy (not a separate worktree)"

export const TERMINAL_ONLY = 'Turn on dashboard execution (orch runner enable) or use the terminal.'
export const TERMINAL_GRANT = 'Run `orch grant --runner` in your terminal.'
export const UPDATE_ORCH = "This orch version can't hand a grant to the runner; update orch."
export const DEPOSIT_UNKNOWN = 'The grant may or may not be with your runner. Revoke it to be sure.'

/** Each closed `deposit_reason` as one sentence; an unknown code reads as `other`. */
const REASONS: Record<DepositReason, string> = {
  'grant.forgotten': 'A newer grant replaced this one before the runner took it.',
  'grant.not_runner': 'The grant is not a runner grant.',
  'grant.verbs': 'The grant does not allow agent work.',
  'grant.expiring': 'The grant has less than 15 minutes left.',
  'rate.limited': 'Too many grants were handed to the runner in the last minute. Wait a moment and issue a new one.',
  'runner.unavailable': 'The runner is not connected.',
  other: 'The runner refused it.',
}
export const depositReason = (code: string | undefined | null): string => REASONS[(code ?? 'other') as DepositReason] ?? REASONS.other

/** `launch.no_grant`'s `detail.reason` as what to do. */
export function noGrantText(reason: AgentBlocked['detail']['reason']): string {
  switch (reason) {
    case 'expiring': return 'Your runner grant has less than 15 minutes left. Issue a new one.'
    case 'scope': return 'Your runner grant covers all tickets, which a member may not use for agents. Issue one for workable tickets.'
    case 'revoked': return 'Your runner grant was revoked or has expired, or you may no longer run agents. Issue a new one.'
    default: return 'Agents need a runner grant on this machine. Issue one.'
  }
}

/** Blocked reasons the dialog explains itself (the host's message is shown beside it as untrusted text). */
export function blockedText(b: AgentBlocked): string | null {
  switch (b.code) {
    case 'runner.disabled':
      return b.detail.missing === 'addon'
        ? 'The start-agent addon is not granted in this workspace.'
        : 'Dashboard execution is off on this machine. Turn it on in your terminal: orch runner enable.'
    case 'runner.unavailable': return 'No runner is connected. Start it in your terminal: orch runner enable.'
    case 'runner.stale': return 'The agent runner is from an older orch version. Restart it in your terminal: orch host enable.'
    case 'launch.claimed': return 'An agent session already claims this ticket.'
    case 'launch.no_grant': return noGrantText(b.detail.reason)
    default: return null
  }
}

/** Where the agent works, in words (the path itself is shown beside it, as it came). */
export function cwdText(p: AgentPreview): string {
  const { kind, repo, ticket } = p.cwd
  if (kind === 'worktree') return p.will_create_worktree ? 'a new worktree' : `the worktree for ${ticket ?? p.ticket ?? 'the ticket'} in ${repo ?? 'its repository'}`
  if (kind === 'repo') return SHARED_COPY
  if (kind === 'workdir') return `the work folder of ${ticket ?? p.ticket ?? 'the ticket'}`
  if (kind === 'workspace') return 'the workspace root'
  return 'not shown'
}

/** The host's login-shell sentence (core launch.py LOGIN_NOTICE, pinned by a server test); the notice is acknowledged on its own. */
export const LOGIN_NOTICE = 'the program is found and started through your login shell; your shell profile runs first and can change what runs'
