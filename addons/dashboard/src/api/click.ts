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
  /** M7b: a runner grant prepared in the deposit-only mode (section 0.8.6 item 6). Absent on every other View. */
  deposit?: boolean
  /** Core's constant `DEPOSIT_LINE`, when `deposit` is true. */
  deposit_line?: string
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
  /** M7b, a runner grant only: where the secret went (the secret itself never leaves the signer). */
  deposit?: DepositOutcome
  /** The request's ULID (`hr_` stripped): ask `grant.deposit_status` with it, never mint another. */
  deposit_id?: string | null
  /** The grant's id (`gr_…`): what Revoke and Forget name. */
  grant?: string | null
  /** H11c-2, only when `deposit` is `rejected`: a closed code, never host text. */
  deposit_reason?: DepositReason
}

export type DepositOutcome = 'confirmed' | 'rejected' | 'unknown'
export type DepositReason = 'grant.forgotten' | 'grant.not_runner' | 'grant.verbs' | 'grant.expiring' | 'rate.limited' | 'runner.unavailable' | 'other'

/** M7b: the runner grant, a click request of its own (never a terminal hand-off). The signer sets label and verbs. */
export interface RunnerGrantAction {
  action: 'runner_grant'
  hours: number
  scope: 'workable' | 'all'
}

/** Every typed action a click request may carry. */
export type ClickAction = HandoffAction | RunnerGrantAction

/** Core's constant (orch.click.protocol DEPOSIT_LINE): a deposit View must carry it, or Sign is not offered. */
export const DEPOSIT_LINE = 'The secret goes to your orch runner on this machine and is never shown'
/** The owner's risk sentence for the runner grant (D-P2-27), verbatim. */
export const DEPOSIT_RISK = 'Anything that takes over the dashboard can give agents standing authority to act for you on tickets for up to 12 hours.'

/** A deposit View is shown with a Sign button only when it says so and carries both constants (the server checks too). */
export const depositViewOk = (view: ClickView): boolean =>
  view.deposit === true && view.deposit_line === DEPOSIT_LINE && view.lines.includes(DEPOSIT_LINE) && view.lines.includes(DEPOSIT_RISK) &&
  view.confirm.kind === 'word' && /^[A-Z][A-Z0-9]{0,15}~[0-9a-f]{8}\/GRANT$/.test(view.confirm.word)

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
export const clickEligible = (a: ClickAction | null): boolean => !!a && CLICK_ACTIONS.has(a.action)

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
  /** Already done: nothing to sign; "Do it again" is an explicit new attempt. */
  | 'again'
  /** The host holds an attempt of this action that cannot be reached from here: nothing is offered until it settles. */
  | 'open'

export interface ClickProblem {
  title: string
  body: string
  next: ClickNext
  /** `rate.limited`: how long to wait. */
  waitMs?: number
  signedAt?: string | null
}

const UNAVAILABLE: Record<string, string> = {
  disabled: 'Click-to-sign is switched off for this workspace.',
  relay_paired: 'This workspace is paired with a relay, so the host will not accept convenience signatures.',
  other_member: 'This workspace has another member, so the host will not accept convenience signatures.',
  phone_device: 'You have another device in this workspace, so the host will not accept convenience signatures.',
  reader_version: 'The host does not understand convenience signatures yet. Update orch-core.',
}

const OFF = ' Fix it, or turn convenience off from the terminal with `orch host convenience off --workspace <prefix>`.'

const REFUSED: Record<string, ClickProblem> = {
  terminal_only: { title: 'Terminal only', body: 'This one cannot be signed with one click.', next: 'terminal' },
  not_prepared_here: { title: 'Not prepared by this dashboard', body: 'The signing helper does not know this request (it may have restarted). Prepare it again.', next: 'prepare' },
  expired: { title: 'Expired', body: 'The request ran out of time before it was signed. Prepare it again.', next: 'prepare' },
  view_changed: { title: 'The review changed', body: 'What you would sign is no longer what you were shown. Nothing was signed. Read the new review.', next: 'review' },
  too_soon: { title: 'Too soon', body: 'The review was on screen for less than a second. Nothing was signed. Press Sign again.', next: 'retry' },
  word: { title: 'Not the word shown', body: 'Type the word exactly as the review shows it. Nothing was signed.', next: 'retry' },
  custody_changed: { title: 'The key moved', body: 'The custody of your device key changed since the review. Nothing was signed. Prepare the request again.', next: 'prepare' },
  busy: { title: 'Busy', body: 'Another signature is in progress. Wait a moment, then press Sign again.', next: 'retry' },
  check_failed: { title: 'A check failed', body: 'The signing helper refused what the host sent (it did not match). Nothing was signed.'+OFF, next: 'terminal' },
  already_signed: { title: 'Already signed from another window', body: 'This request was signed with a different review. Nothing more can be signed on it; this shows where it stands.', next: 'check' },
  record_unavailable: { title: 'The signing record is unreadable', body: 'The signing helper cannot read its own record of what it signed, so it refuses to sign. Nothing was signed here. Fix it, or turn convenience off from the terminal with `orch host convenience off --workspace <prefix>`; this workspace then signs in the terminal.', next: 'terminal' },
  uncertain: { title: 'The outcome is not certain yet', body: 'The signature may or may not have reached the host. It settles by itself; do not sign again until this says it is settled.', next: 'check' },
}

/**
 * A sign whose outcome the browser cannot know: the answer was lost (a failed fetch) or came from a server that failed
 * without saying anything (5xx, no catalog body). The signature may be at the host: wait for it, never offer a second one.
 */
export const unknownOutcome = (e: unknown): boolean => !(e instanceof ApiError) || (e.status >= 500 && e.exit === undefined)

/** Only server-sourced record facts; never the current form's values. */
export function alreadySigned(e: Pick<ApiError, 'signed_at' | 'op' | 'summary'>): string {
  const thing = e.op === 'log' ? 'comment with the same text' : e.op === 'answer' ? 'answer' : e.op ? `${e.op} action` : 'action'
  const at = e.signed_at ? Date.parse(e.signed_at) : NaN
  const when = Number.isFinite(at) ? `${Math.max(0, Math.floor((Date.now() - at) / 60_000))} min ago` : '(signing time unavailable from this host)'
  return `A similar ${thing} was signed ${when}. ${e.summary ?? 'Signed content unavailable from this host.'} Do it again only if you intend a second signature.`
}

/** Every code and reason of section 0.8.6 as one honest message and the one thing to do next. Unknown codes are shown as they are. */
export function clickProblem(e: unknown): ClickProblem {
  if (!(e instanceof ApiError)) return { title: 'Could not reach the dashboard server', body: 'Try again.', next: 'retry' }
  switch (e.code) {
    case 'human.click_refused':
      return REFUSED[e.reason ?? ''] ?? { title: 'Refused', body: `The signing helper refused this (${e.reason ?? 'no reason'}). Nothing was signed.`, next: 'terminal' }
    case 'sign.convenience_unavailable':
      return { title: 'Click-to-sign is not available here', body: `${UNAVAILABLE[e.reason ?? ''] ?? 'The host does not allow it for this workspace.'}${OFF}`, next: 'terminal' }
    case 'intent.done':
      return { title: 'Already done', body: alreadySigned(e), next: 'again', signedAt: e.signed_at }
    case 'intent.failed':
      return { title: 'The earlier attempt did not land', body: e.message, next: 'prepare' }
    case 'lock.busy':
    case 'host.busy':
      return { title: 'Busy', body: 'The host is busy. Try again shortly.', next: 'retry' }
    case 'intent.expired':
      return { title: 'This attempt is too old; start again', body: e.message, next: 'prepare' }
    case 'intent.ended':
      return { title: 'The earlier attempt ended', body: `${e.message} Nothing was signed. Start again to make a new request.`, next: 'prepare' }
    case 'intent.blocked':
      return { title: 'The earlier attempt ended', body: e.message, next: 'prepare' }
    case 'intent.open':
      return { title: 'An attempt for this is already open', body: e.message || 'The host holds an open request for this action. Wait until it ends or expires.', next: 'open' }
    case 'sign.click_only':
      return { title: 'This workspace signs in the dashboard', body: e.message, next: 'open' }
    case 'sign.convenience_readers':
      return { title: 'This would add a reader', body: [e.message, e.hint, typeof e.fix === 'string' ? e.fix : e.fix ? JSON.stringify(e.fix) : null].filter(Boolean).join(' '), next: 'terminal' }
    case 'rate.limited':
      return { title: 'Too many signatures', body: `The signing helper allows only so many signatures per minute and hour. Try again in ${Math.max(1, Math.ceil((e.retry_after_ms ?? 1000) / 1000))} s.`, next: 'retry', waitMs: e.retry_after_ms ?? 1000 }
    case 'custody.no_key':
      return { title: 'No key on this computer', body: 'There is no device key for this workspace in convenience custody here. '+OFF, next: 'terminal' }
    case 'custody.unavailable':
      return { title: 'The key store is not available', body: 'The key store is locked or refused access. Unlock it.'+OFF, next: 'terminal' }
    case 'host.version_mismatch':
      return { title: 'Update orch-core', body: 'The signing helper and the host are different versions of orch-core. Update orch-core and restart the dashboard.', next: 'terminal' }
    case 'click.contract':
      return { title: 'Update orch-core', body: 'This dashboard and its signing helper do not speak the same version. Update orch-core and restart the dashboard.', next: 'terminal' }
    case 'click.unavailable':
      return { title: 'The signing helper is not running', body: 'It stopped, so open sign requests are gone. Start again.', next: 'prepare' }
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
