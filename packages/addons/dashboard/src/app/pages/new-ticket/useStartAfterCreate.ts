import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { agentsApi, type AgentArgs } from '@/api/agents'
import type { Run } from '@/api/runs'
import { ApiError } from '@/api/types'
import type { StartInitial } from '@/app/terminal/real/StartAgentDialog'
import { compareStart, notStarted, type Reviewed } from './startAfterCreate'

const message = (e: unknown) => (e instanceof ApiError ? e.message : 'The dashboard server did not answer.')

export type StartOutcome =
  | { kind: 'started'; run: Run }
  /** Not started on its own: the person reads the host's preview in the Start agent dialog and starts there (or not). */
  | { kind: 'ask'; reason: string; initial: StartInitial }

export interface StartRequest {
  ws: string
  requestId?: string
  /** The key of the `new` request THIS form prepared (ownKey): never typed, never read from a list. */
  key: string
  reviewed: Reviewed
  /** One idem per review: a retry of the same start returns the same run. */
  idem: string
  /** Grants that existed before the ticket was signed (a grant signed with it must be a different one). */
  priorGrantIds: readonly string[]
  /** The review showed the full "runs as you" notice and the person signed with it on screen: that counts as reading it,
   *  as starting from the Start agent dialog with the notice open does. Saved as read once the agent started. */
  noticeRead?: boolean
}

/**
 * After every signed step has landed: preview the start for the ticket's own key, compare it with what the review
 * showed, and start only when they are equal and nothing needs reading first. One preview, at most one start, no loop.
 */
export async function startAfterCreate(r: StartRequest): Promise<StartOutcome> {
  if (r.requestId) {
    try {
      const { run } = await agentsApi.startAfter(r.ws, r.requestId)
      if (r.noticeRead) await agentsApi.acknowledge(r.ws, 'start_agent').catch(() => {})
      return { kind: 'started', run }
    } catch (e) {
      return { kind: 'ask', reason: notStarted(r.key, message(e)), initial: { ...r.reviewed.choice, note: message(e), afterRequest: r.requestId } }
    }
  }
  const { choice } = r.reviewed
  const args: AgentArgs = { ticket: r.key, harness: choice.harness, mode: choice.mode, ...(choice.model ? { model: choice.model } : {}), worktree: true }
  const ask = (reason: string, idem?: string): StartOutcome => ({
    kind: 'ask',
    reason: notStarted(r.key, reason),
    initial: { harness: choice.harness, mode: choice.mode, ...(choice.model ? { model: choice.model } : {}), note: notStarted(r.key, reason), ...(idem ? { idem } : {}) },
  })
  let preview
  try {
    preview = await agentsApi.preview(r.ws, args)
  } catch (e) {
    return ask(message(e))
  }
  let ack = { start_agent: false, login_shell: false }
  try {
    const got = (await agentsApi.riskNotices(r.ws)).acknowledged
    ack = { start_agent: got.start_agent === true, login_shell: got.login_shell === true }
  } catch { /* unreadable counts as not acknowledged */ }
  // Only the "runs as you" notice can be read in the review; the login-shell notice comes with the preview and still goes to the dialog.
  const seen = { ...ack, start_agent: ack.start_agent || r.noticeRead === true }
  const verdict = compareStart(preview, r.reviewed, args, seen, r.priorGrantIds)
  if (!verdict.ok) return ask(verdict.reason)
  try {
    const { run } = await agentsApi.start(r.ws, args, preview.spec as string, r.idem)
    if (seen.start_agent && !ack.start_agent) await agentsApi.acknowledge(r.ws, 'start_agent').catch(() => { /* not saved: shown in full again next time */ })
    return { kind: 'started', run }
  } catch (e) {
    // A lost answer may still have started the agent: the dialog then reuses this idem, so Start returns that run.
    return ask(message(e), !(e instanceof ApiError) || e.status >= 500 ? r.idem : undefined)
  }
}

export type AfterState =
  | { kind: 'idle' }
  | { kind: 'running'; key: string }
  | { kind: 'ask'; key: string; reason: string; initial: StartInitial }
  /** The ticket landed and its agent access did not: the person is told and can start from the dialog. */
  | { kind: 'no-access'; key: string; initial: StartInitial }

/** The state of what happens after the ticket exists (kept in memory only: a reload never resumes a start). */
export function useStartAfterCreate() {
  const qc = useQueryClient()
  const [state, setState] = useState<AfterState>({ kind: 'idle' })
  const run = useCallback(async (r: StartRequest): Promise<StartOutcome> => {
    setState({ kind: 'running', key: r.key })
    const out = await startAfterCreate(r)
    if (out.kind === 'started') void qc.invalidateQueries({ queryKey: ['runs', r.ws] })
    setState(out.kind === 'ask' ? { kind: 'ask', key: r.key, reason: out.reason, initial: out.initial } : { kind: 'idle' })
    return out
  }, [qc])
  const noAccess = useCallback((key: string, initial: StartInitial) => setState({ kind: 'no-access', key, initial }), [])
  const ask = useCallback((key: string, initial: StartInitial) => setState({ kind: 'ask', key, reason: '', initial }), [])
  const reset = useCallback(() => setState({ kind: 'idle' }), [])
  return { state, run, noAccess, ask, reset }
}
