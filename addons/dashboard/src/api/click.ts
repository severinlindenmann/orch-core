// Click-to-sign in the dashboard (H5c, host contract section 0.8.6). The signer is core's own child process; the key
// is never here. The dashboard (this page and its server) is trusted to show the signer's View and to ask for the
// signature only after the person confirmed: the card and every panel say so.
import type { Workspace } from './types'
import { ApiError } from './types'
import type { HandoffAction } from './handoff'

/** Verbatim (owner decision, 12 Oct 2026). Also the first line of every sign panel. */
export const CLICK_RISK = 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.'

/** The panel's Sign button is off until the View has been on screen, visible and focused, for this long (the signer's own dwell is 1 s). */
export const ARM_MS = 1000

export interface ClickPrepared {
  request_id: string
  expires_at: string
  op: string
  confirm: 'click' | 'word'
}

/** The signer's View, as it came (the server adds nothing). `lines` are core's terminal review, already escaped. */
export interface ClickView {
  risk: string
  op: string
  workspace: { id: string; name: string; prefix: string }
  fingerprint: string
  lines: string[]
  checks: { repo: string; clean: boolean; attested_by: string; at: string }[]
  confirm: { kind: 'click' } | { kind: 'word'; word: string }
  expires_at: string
  auth: string
  device: string
  artifacts: 'checked' | 'unseen' | null
}

export interface ClickViewAnswer {
  view: ClickView
  view_digest: string
}

/** Where a request stands at the host (read by the server through its own link). */
export type ClickHostState = 'prepared' | 'bound' | 'submitted' | 'cancelled' | 'expired' | 'unknown'

export interface ClickSigned {
  state: 'submitted'
  duplicate: boolean
  key?: string
  seq?: number
}

/** The flag of a workspace: the signer says click-to-sign is on and it can sign there now (real host only). */
export const clickReady = (w: Pick<Workspace, 'click'> | undefined | null): boolean => !!w?.click && w.click.enabled && w.click.available

/**
 * Which typed actions are offered a Sign button: the ones the signer's allowlist serves as click or click + word.
 * `grant` is not an action of the dashboard at all (terminal only); anything not listed here is terminal only.
 */
const CLICK_ACTIONS: ReadonlySet<string> = new Set<HandoffAction['action']>([
  'approve', 'request_changes', 'verdict', 'answer', 'comment', 'label', 'close', 'reopen', 'move', 'new', 'details', 'section',
  'settings_set', 'policy_set', 'member_add', 'member_role', 'member_remove', 'grant_revoke',
])
export const clickEligible = (a: HandoffAction | null): boolean => !!a && CLICK_ACTIONS.has(a.action)

export type ClickNext =
  /** Sign again (the review is unchanged). */
  | 'retry'
  /** Read the review again: it changed. */
  | 'review'
  /** Start over with a new request. */
  | 'prepare'
  /** Click cannot work here: the terminal path. */
  | 'terminal'
  /** The outcome is unknown: look at the ticket before anything else. */
  | 'check'

export interface ClickProblem {
  title: string
  body: string
  next: ClickNext
  /** `rate.limited`: how long to wait. */
  waitMs?: number
}

const UNAVAILABLE: Record<string, string> = {
  disabled: 'Click-to-sign is switched off for this workspace.',
  relay_paired: 'This workspace is paired with a relay, so the host will not accept convenience signatures.',
  other_member: 'This workspace has another member, so the host will not accept convenience signatures.',
  phone_device: 'You have another device in this workspace, so the host will not accept convenience signatures.',
  reader_version: 'The host does not understand convenience signatures yet. Update orch-core.',
}

const REFUSED: Record<string, ClickProblem> = {
  terminal_only: { title: 'Terminal only', body: 'This one is signed in your terminal, not here.', next: 'terminal' },
  not_prepared_here: { title: 'Not prepared by this dashboard', body: 'The signing helper does not know this request (it may have restarted). Sign it in the terminal instead.', next: 'terminal' },
  expired: { title: 'Expired', body: 'The request ran out of time before it was signed. Prepare it again.', next: 'prepare' },
  view_changed: { title: 'The review changed', body: 'What you would sign is no longer what you were shown. Nothing was signed. Read the new review.', next: 'review' },
  too_soon: { title: 'Too soon', body: 'The review was on screen for less than a second. Nothing was signed. Press Sign again.', next: 'retry' },
  word: { title: 'Not the word shown', body: 'Type the word exactly as the review shows it. Nothing was signed.', next: 'retry' },
  custody_changed: { title: 'The key moved', body: 'The custody of your device key changed since the review. Nothing was signed. Prepare the request again.', next: 'prepare' },
  busy: { title: 'Busy', body: 'Another signature is in progress. Wait a moment, then press Sign again.', next: 'retry' },
  check_failed: { title: 'A check failed', body: 'The signing helper refused what the host sent (it did not match). Nothing was signed. You can sign in the terminal instead.', next: 'terminal' },
  uncertain: { title: 'The outcome is not certain yet', body: 'The signature may or may not have reached the host. It settles by itself; do not sign again until this says it is settled.', next: 'check' },
}

/**
 * A sign whose outcome the browser cannot know: the answer was lost (a failed fetch) or came from a server that failed
 * without saying anything (5xx, no catalog body). The signature may be at the host: wait for it, never offer a second one.
 */
export const unknownOutcome = (e: unknown): boolean => !(e instanceof ApiError) || (e.status >= 500 && e.exit === undefined)

/** Every code and reason of section 0.8.6 as one honest message and the one thing to do next. Unknown codes are shown as they are. */
export function clickProblem(e: unknown): ClickProblem {
  if (!(e instanceof ApiError)) return { title: 'Could not reach the dashboard server', body: 'Try again, or sign in the terminal instead.', next: 'retry' }
  switch (e.code) {
    case 'human.click_refused':
      return REFUSED[e.reason ?? ''] ?? { title: 'Refused', body: `The signing helper refused this (${e.reason ?? 'no reason'}). Nothing was signed.`, next: 'terminal' }
    case 'sign.convenience_unavailable':
      return { title: 'Click-to-sign is not available here', body: `${UNAVAILABLE[e.reason ?? ''] ?? 'The host does not allow it for this workspace.'} Sign in the terminal instead.`, next: 'terminal' }
    case 'click.unsettled':
      return { title: 'The last signature of this is not settled', body: 'It settles by itself. This shows its state until it does.', next: 'check' }
    case 'sign.convenience_readers':
      return { title: 'This would add a reader', body: e.message, next: 'terminal' }
    case 'rate.limited':
      return { title: 'Too many signatures', body: `The signing helper allows only so many signatures per minute and hour. Try again in ${Math.max(1, Math.ceil((e.retry_after_ms ?? 1000) / 1000))} s.`, next: 'retry', waitMs: e.retry_after_ms ?? 1000 }
    case 'custody.no_key':
      return { title: 'No key on this computer', body: 'There is no device key for this workspace in convenience custody here. Sign in the terminal instead.', next: 'terminal' }
    case 'custody.unavailable':
      return { title: 'The key store is not available', body: 'The key store is locked or refused access. Unlock it, or sign in the terminal instead.', next: 'terminal' }
    case 'host.version_mismatch':
      return { title: 'Update orch-core', body: 'The signing helper and the host are different versions of orch-core. Update orch-core and restart the dashboard.', next: 'terminal' }
    case 'click.contract':
      return { title: 'Update orch-core', body: 'This dashboard and its signing helper do not speak the same version. Update orch-core and restart the dashboard.', next: 'terminal' }
    case 'click.unavailable':
      return { title: 'The signing helper is not running', body: 'It stopped, so open sign requests are gone. Sign in the terminal instead, or start again.', next: 'terminal' }
    case 'human.request_expired':
      return { title: 'Expired', body: 'The request ran out of time before it was signed. Prepare it again.', next: 'prepare' }
    case 'human.request_state':
      return { title: 'Already settled', body: 'The host says this request is already signed, cancelled or expired. The page shows the current state.', next: 'prepare' }
    case 'host.unavailable':
      return { title: 'The host does not answer', body: 'Nothing was signed. Try again in a moment.', next: 'retry' }
    case 'not_found':
      return { title: 'Not found', body: 'The dashboard server does not know this request any more. Prepare it again.', next: 'prepare' }
    default:
      return { title: 'Refused', body: e.message || `The host refused this (${e.code}). Nothing was signed.`, next: e.retryable ? 'retry' : 'terminal' }
  }
}
