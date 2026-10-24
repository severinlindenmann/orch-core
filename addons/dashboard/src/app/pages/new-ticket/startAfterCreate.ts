// "Create and start agent": what the review showed about the start, and the rule that lets the start go ahead on its own.
//
// The start is never part of the signature. After every signed step has landed, the dashboard asks the host to preview
// the start for the ticket's own key and starts only when that preview says what the page said before the signing. Any
// difference, or anything that needs the person to read it first, leaves the decision to them in the Start agent dialog.
import { LOGIN_NOTICE, type AgentArgs, type AgentMode, type AgentPreview, type Harness } from '@/api/agents'
import { MAX_SEQUENCE_STEPS } from '@/api/handoff'
import type { ModelTable } from '@/api/modelRouting'
import type { RunnerInfo } from '@/api/runs'

/** The choice behind "Agent: Claude Code · Do the work · Opus". `model` is empty when the host picks it from Settings. */
export interface AgentChoiceValue {
  harness: Harness
  mode: AgentMode
  model: string
}

export const DEFAULT_CHOICE: AgentChoiceValue = { harness: 'claude-code', mode: 'work', model: '' }

/**
 * A runner grant counts as startable by the host's own rule (`whoami.runner.grant.startable`, with `startable_until`):
 * the floor, member scope, a revoked grant and a role change are the host's to judge. A host that does not say yet is
 * read with a 20-minute floor, failing toward asking for access.
 */
export const MIN_GRANT_MS = 20 * 60_000

export function usableGrant(runner: Pick<RunnerInfo, 'grant'> | null | undefined, now: number): { id: string; until: string } | null {
  const g = runner?.grant
  if (!g) return null
  if (typeof g.startable === 'boolean') {
    const stop = g.startable_until ? Date.parse(g.startable_until) : null
    return g.startable && (stop === null || !Number.isFinite(stop) || stop > now) ? { id: g.id, until: g.until } : null
  }
  const end = Date.parse(g.until)
  return Number.isFinite(end) && end - now >= MIN_GRANT_MS ? { id: g.id, until: g.until } : null
}

/** What the host will use for the model: the person's choice, else the signed routing table, else the agent's own default. */
export interface ShownModel {
  model: string | null
  source: 'chosen' | 'workspace' | 'default'
}

export function shownModel(choice: AgentChoiceValue, table: ModelTable | null | undefined, routing: boolean): ShownModel {
  if (choice.model) return { model: choice.model, source: 'chosen' }
  const routed = routing ? table?.[choice.harness]?.[choice.mode] : undefined
  return routed ? { model: routed, source: 'workspace' } : { model: null, source: 'default' }
}

/** What the page showed before the signing: it is what the preview must equal. `grantId` is null while the grant is yet to be signed. */
export interface Reviewed {
  choice: AgentChoiceValue
  model: ShownModel
  /** The usable grant the page showed (its id), or null when the new grant is the one that was signed last. */
  grantId: string | null
}

export type Verdict = { ok: true } | { ok: false; reason: string }

const no = (reason: string): Verdict => ({ ok: false, reason })

/**
 * The fail-closed comparison. The host's preview must not be blocked, must carry a spec, and must equal the review in
 * harness, mode, model, model source, and no new worktree may be created. The grant must be the one shown, or (when the
 * grant was signed with the ticket) any grant that did not exist before. Warnings: only the login-shell notice, and only
 * once it has been acknowledged (an unread notice goes to the dialog, which shows it in full).
 */
export function compareStart(preview: AgentPreview, reviewed: Reviewed, args: AgentArgs, ack: { start_agent: boolean; login_shell: boolean }, priorGrantIds: readonly string[] = []): Verdict {
  if (preview.blocked) return no(preview.blocked.message ?? preview.blocked.code ?? 'the host would not start it')
  if (!preview.spec) return no('the host gave no start check')
  if (args.harness !== reviewed.choice.harness || args.mode !== reviewed.choice.mode) return no('the agent or what it does changed')
  if ((args.model ?? '') !== reviewed.choice.model) return no('the model changed')
  if ((preview.model ?? null) !== reviewed.model.model || (preview.model_source ?? 'default') !== reviewed.model.source) return no('the model differs from the one shown')
  if (preview.will_create_worktree) return no('a new project folder would be created')
  // The review says "in the project folder": the workspace's own folder, or the ticket's own work folder inside it
  // (.orch-work/<KEY>, a ticket without a codebase: run 6, #1071). Not a repository's shared copy or a worktree.
  const inProject = preview.cwd.kind === 'workspace' || (preview.cwd.kind === 'workdir' && preview.cwd.ticket === args.ticket)
  if (!inProject) return no('it would not run in the project folder')
  // A new ticket's work folder must be new or empty: something already in it (settings, notes) was not made for this ticket.
  if (preview.cwd.kind === 'workdir' && !(preview.workdir && (!preview.workdir.exists || preview.workdir.empty))) return no('the ticket folder already has files in it')
  if (preview.display === null || preview.display_hidden || preview.cwd_path === null || preview.cwd_path_hidden) return no('the command or the folder cannot be shown')
  if (!preview.grant) return no('there is no agent access')
  if (reviewed.grantId !== null ? preview.grant.id !== reviewed.grantId : priorGrantIds.includes(preview.grant.id)) return no('the agent access differs from the one shown')
  if (!ack.start_agent) return no('the notice that agents run as you has not been read yet')
  const others = preview.warnings.filter((w) => w !== LOGIN_NOTICE)
  if (others.length) return no(others[0])
  if (preview.warnings.includes(LOGIN_NOTICE) && !ack.login_shell) return no('the notice about your login shell has not been read yet')
  return { ok: true }
}

/** Signatures of the new ticket without the agent access: what `handoffNewTicket` counts. The access is one more, last. */
export const stepsWithAccess = (steps: number, needsGrant: boolean): number => steps + (needsGrant ? 1 : 0)
export const fitsWithAccess = (steps: number, needsGrant: boolean): boolean => stepsWithAccess(steps, needsGrant) <= MAX_SEQUENCE_STEPS

/** The one sentence under a start that did not go ahead on its own. */
export const notStarted = (key: string, reason: string): string => `Created ${key}. The agent was not started: ${reason}`
