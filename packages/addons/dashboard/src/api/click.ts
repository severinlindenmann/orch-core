// Click-to-sign in the dashboard (H5c, host contract section 0.8.6). The signer is core's own child process; the key
// is never here. The dashboard (this page and its server) is trusted to show the signer's View and to ask for the
// signature only after the person confirmed: the card and every panel say so.
import type { Workspace } from './types'
import { ApiError } from './types'
import type { HandoffAction } from './handoff'
import { DEV_REFUSED, devRefusedSentence, refusalOf, refusalSentence } from './refusals'

/** Verbatim risk notice: enable time and Settings, with a tooltip in sign dialogs. */
export const CLICK_RISK = 'Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.'

/** The panel's Sign button is off until the View has been on screen, visible and focused, for this long (the signer's own dwell is 1 s). */
export const ARM_MS = 1000

/** The signer's card for a workspace (`/click/status`), and whether it can switch signing on and off (section 0.8.6 item 3a). */
export interface ClickCard {
  risk_ack?: string | null
  custody: 'convenience' | 'passphrase' | 'none'
  enabled: boolean
  available: boolean
  problem?: string
  /** C17: the configured idle limit in hours, on or off (One click switches itself off after this long without use). */
  idle_hours?: number
  switchable?: boolean
  /** The device key (`d_` + 32 hex) the switch would move into this computer's key store. */
  device?: string
}

/**
 * The switch: `on` proves the person's CURRENT passphrase (the key must be in passphrase custody now); `off` turns the
 * flag off and, with a NEW passphrase (V3-D31), moves the key back under it in the same step (core accepts it only
 * while signing is on). The new passphrase is never sent in the clear: it is sealed to the signer's key (C12).
 */
export type ClickSwitch =
  | { on: true; passphrase: string }
  | { on: true; resume: true }
  | { on: false }
  | { on: false; request_id: string; view_digest: string; sealed: { 'custody.passphrase': import('./sealed').Envelope } }

/** The body of `POST click/github` (#1198 C16): the GitHub token, sealed to the signer's one-use key. */
export interface GithubSave {
  request_id: string
  view_digest: string
  sealed: { 'github.token': import('./sealed').Envelope }
}
/** Whether a GitHub token is saved; `null` when the GitHub add-on is off or asks for no key. */
export type GithubCredential = 'stored' | 'missing' | null

/** The signer's one-use key for the NEW passphrase of "off" (#1198 C12, sealed purpose custody.passphrase). */
export interface CustodySealOffer {
  request_id: string
  view_digest: string
  seal: { v: 1; alg: string; pk: string; purposes: string[] }
  expires_in: number
}

export interface ClickPrepared {
  request_id: string
  expires_at: string
  op: string
  confirm: 'click'
}

/** The signer's View, as it came (the server adds nothing). `lines` are core's terminal review, already escaped. */
export interface ClickView {
  headline?: string
  content?: import('./signing').SignContent
  risk: string
  op: string
  workspace: { id: string; name: string; prefix: string }
  fingerprint: string
  lines: string[]
  checks: { repo: string; clean: boolean; attested_by: string; at: string }[]
  confirm: { kind: 'click' }
  expires_at: string
  auth: string
  device: string
  artifacts: 'checked' | 'unseen' | null
  /** M7b: a runner grant prepared in the deposit-only mode (section 0.8.6 item 6). Absent on every other View. */
  deposit?: boolean
  /** Core's constant `DEPOSIT_LINE`, when `deposit` is true. */
  deposit_line?: string
  /** #1198 C7: a grant whose secret is shown once in this page. `for` names the page key this review was made for. */
  seal?: { v: number; alg: string; purposes: string[]; for?: string; pk?: string }
  /** #1198 C10: a hub step (core's constants, when the View asks for the backup key). */
  backup_key_line?: string
}

export interface ClickViewAnswer {
  view: ClickView
  view_digest: string
}

/** Where a request stands at the host (read by the server through its own link). */
export type ClickHostState = 'prepared' | 'bound' | 'submitted' | 'cancelled' | 'expired' | 'unknown'

/** `click/state`: where the request stands. For a stopped sequence also the ticket that exists and how far the run got. */
export interface ClickState {
  id: string
  state: ClickHostState
  key?: string
  progress?: { done: number; of: number; last?: 'grant' }
}

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
  /** #1198 C7, agent access only: the secret sealed to this page's key (ciphertext; null if it could not be delivered). */
  reveal?: { purpose: 'grant.secret'; envelope: import('./sealed').Envelope } | null
}

export type DepositOutcome = 'confirmed' | 'rejected' | 'unknown'
export type DepositReason = 'grant.forgotten' | 'grant.not_runner' | 'grant.verbs' | 'grant.expiring' | 'rate.limited' | 'runner.unavailable' | 'other'

/** M7b: the runner grant, a click request, or a terminal hand-off with runner set by this server, never by the browser. The signer sets label and verbs. */
export type RunnerGrantAction = Extract<HandoffAction, { action: 'runner_grant' }>

/**
 * #1198 C7: agent access for a tool outside the dashboard. The secret comes back sealed to `reveal_key`, the page's
 * one-use public key, which is sent at prepare so the review the person approves is of that key.
 */
export type AgentAccessAction = { action: 'agent_access'; hours: number; scope: 'workable' | 'all'; label?: string; reveal_key: string }

/** #1198 C10: join a hub (selective only; a full join is the Terminal's) or leave it, with the backup key sealed to the signer. */
export type HubJoinAction = { action: 'hub_join'; hub_card: string; hub_id: string; mode?: 'selective' | 'full'; owner_user?: string }
export type HubModeFullAction = { action: 'hub_mode_full'; hub_card: string; hub_id: string }
export type HubLeaveAction = { action: 'hub_leave' }

/** Every typed action a click request may carry. */
export type ClickAction = HandoffAction | RunnerGrantAction | AgentAccessAction | HubJoinAction | HubModeFullAction | HubLeaveAction

/** Core's constant (orch.click.protocol DEPOSIT_LINE): a deposit View must carry it, or Sign is not offered. */
export const DEPOSIT_LINE = 'The secret goes to your orch runner on this machine and is never shown'
/** The owner's risk sentence for the runner grant (D-P2-27), verbatim. */
export const DEPOSIT_RISK = 'Anything that takes over the dashboard can give agents standing authority to act for you on tickets for as long as the grant lasts (up to 7 days, the length you choose); you can stop it at any time.'

/**
 * A deposit View is shown with a Sign button only when it says so, carries both constants, and is of this workspace.
 * The server checks too.
 */
export const depositViewOk = (view: ClickView, ws: string): boolean =>
  view.workspace?.id === ws && view.confirm.kind === 'click' &&
  view.deposit === true && view.deposit_line === DEPOSIT_LINE && view.lines.includes(DEPOSIT_LINE) && view.lines.includes(DEPOSIT_RISK)

/** Core's constants (orch.click.protocol REVEAL_LINE, REVEAL_RISK): a shown-secret View lacking either is not offered. */
export const REVEAL_LINE = 'The secret is shown once, on this page, only to you. orch keeps only a fingerprint of it'
export const REVEAL_RISK = 'The secret will be shown once in this browser. Anyone who controls this dashboard could read it.'

/** The page key as the signer's View names it: the full SHA-256 (64 hex) of its base64url text (golden vectors: packages/core/tests/click_reveal_fingerprint.json). */
export async function keyFingerprint(publicKeyB64: string): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(publicKeyB64)))
  return Array.from(d, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** A shown-secret View is offered a Sign button only when it is for THIS page's key, the grant.secret purpose, both constants and this workspace. */
export const revealViewOk = async (view: ClickView, ws: string, publicKeyB64: string): Promise<boolean> =>
  view.workspace?.id === ws && view.confirm.kind === 'click' && view.deposit !== true &&
  view.seal?.purposes?.length === 1 && view.seal.purposes[0] === 'grant.secret' && view.seal.for === (await keyFingerprint(publicKeyB64)) &&
  view.lines.includes(REVEAL_LINE) && view.lines.includes(REVEAL_RISK)

/** Core's constants (orch.click.protocol BACKUP_KEY_LINE, BACKUP_KEY_RISK): a hub View lacking either is not offered. */
export const BACKUP_KEY_LINE = 'This step also needs your backup key. You type it once here; orch does not keep it'

/** A hub View gets the backup-key field only when it asks for exactly `hub.backup_key` with the signer's key, carries the backup-key line (`BACKUP_KEY_LINE`, in `lines` and as `backup_key_line`) and is of this workspace. */
/** Run-9 feedback 6: a hub step whose person key the signer kept asks for nothing sealed: no seal, no backup-key line. */
export const plainHubViewOk = (view: ClickView, ws: string): boolean =>
  view.workspace?.id === ws && view.confirm.kind === 'click' && view.deposit !== true && !view.seal && !view.backup_key_line

export const backupViewOk = (view: ClickView, ws: string): boolean =>
  view.workspace?.id === ws && view.confirm.kind === 'click' && view.deposit !== true &&
  view.seal?.purposes?.length === 1 && view.seal.purposes[0] === 'hub.backup_key' && typeof view.seal.pk === 'string' && view.seal.pk.length === 87 &&
  view.lines.includes(BACKUP_KEY_LINE) && view.backup_key_line === BACKUP_KEY_LINE

/** #1198 C13: the signer's review of "Replace this device's key" and its one-use key for the two typed secrets. */
export const ROTATE_LINE =
  'This step also needs your recovery code and a passphrase for the new key. You type them once here; orch does not keep them'
export interface RotateOffer {
  request_id: string
  view_digest: string
  expires_in: number
  view: {
    op: 'device.rotate'
    workspace: { id: string; name: string; prefix: string }
    old: string
    new: string
    lines: string[]
    confirm: { kind: 'click' }
    rotate_line: string
    seal: { v: 1; alg: string; pk: string; purposes: string[] }
  }
}
/** The server answers `{ done: true, ...card }` instead when an earlier run had landed the event and only the install was left. */
export type RotateBegin = RotateOffer | ({ done: true } & ClickCard)
export const rotateOfferOk = (o: RotateBegin, ws: string): o is RotateOffer =>
  !('done' in o) && o.view?.op === 'device.rotate' && o.view.workspace?.id === ws && o.view.confirm?.kind === 'click' &&
  o.view.seal?.pk?.length === 87 && o.view.seal.purposes.length === 2 && o.view.seal.purposes[0] === 'device.recovery_code' &&
  o.view.seal.purposes[1] === 'custody.passphrase' && o.view.lines.includes(ROTATE_LINE) && o.view.old !== o.view.new

/** The flag of a workspace: the signer says click-to-sign is on and it can sign there now (real host only). */
export const clickReady = (w: Pick<Workspace, 'click'> | undefined | null): boolean => !!w?.click && w.click.enabled && w.click.available

/**
 * Which typed actions are offered a Sign button: the ones the signer's allowlist serves as click.
 * The runner grant and the shown-secret agent access are click requests too; anything not listed here is terminal only.
 */
const CLICK_ACTIONS: ReadonlySet<string> = new Set<ClickAction['action']>([
  'accept_criterion', 'withdraw_acceptance', 'mark_task_done',
  'approve', 'request_changes', 'verdict', 'answer', 'comment', 'label', 'close', 'reopen', 'move', 'new', 'new_sequence', 'edit_sequence', 'quick_start', 'details', 'section',
  'settings_set', 'repo_set', 'repo_remove', 'policy_set', 'member_add', 'member_role', 'member_remove', 'device_remove', 'grant_revoke', 'runner_grant', 'agent_access', 'hub_join', 'hub_leave', 'addon_grant', 'market_grant', 'addon_disable', 'addon_purge', 'addon_settings', 'addon_enable', 'publish_setup', 'publish_share', 'followup_cancel', 'followup_reschedule', 'followup_undo',
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
  /** Raw host text (ids, hashes) kept out of the sentence: shown folded under "Technical details". */
  details?: string
  /** H12d-2: the device ids the refusal names (shown by label through `RefusalDevices`, never as ids). */
  devices?: string[]
  /** The switch moved after the page chose its way: a new request follows the host's current answer (Try again). */
  stale?: boolean
}

/** The placeholder the sentences carry for the workspace prefix; `withPrefix` fills it in where the page knows the workspace. */
export const PREFIX_PLACEHOLDER = '<prefix>'
export const withPrefix = (text: string, prefix: string | undefined): string =>
  text.split(PREFIX_PLACEHOLDER).join(prefix || '<your workspace prefix>')

const OFF = ' Fix it, or sign in the terminal: turn convenience off with `orch host convenience off --workspace <prefix>`, then move the key back with `orch keys custody passphrase --workspace <prefix> --in-place`.'

const REFUSED: Record<string, ClickProblem> = {
  terminal_only: { title: 'Terminal only', body: 'This one cannot be signed with one click.', next: 'terminal' },
  not_prepared_here: { title: 'Not prepared by this dashboard', body: 'The signing helper does not know this request (it may have restarted). Prepare it again.', next: 'prepare' },
  expired: { title: 'Expired', body: 'The request ran out of time before it was signed. Prepare it again.', next: 'prepare' },
  view_changed: { title: 'The review changed', body: 'What you would sign is no longer what you were shown. Nothing was signed. Read the new review.', next: 'review' },
  too_soon: { title: 'Too soon', body: 'The review was on screen for less than a second. Nothing was signed. Press the button again.', next: 'retry' },
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
  const thing = e.op === 'log' ? 'comment with the same text' : e.op === 'answer' ? 'answer' : e.op === 'addon.grant' || e.op === 'addon.disable' ? 'add-on change' : 'action'
  const at = e.signed_at ? Date.parse(e.signed_at) : NaN
  const when = Number.isFinite(at) ? `${Math.max(0, Math.floor((Date.now() - at) / 60_000))} min ago` : '(signing time unavailable from this host)'
  // The host's record of what was signed is for the technical details (`alreadyDetails`), never for the sentence.
  return `A similar ${thing} was signed ${when}. Do it again only if you intend a second signature.`
}

/** The host's record of the earlier signature, for a folded "Technical details" block only. */
export const alreadyDetails = (e: Pick<ApiError, 'summary'>): string | undefined => e.summary || undefined

/** Every code and reason of section 0.8.6 as one honest message and the one thing to do next. Unknown codes are shown as they are. */
export function clickProblem(e: unknown): ClickProblem {
  if (!(e instanceof ApiError)) return { title: 'Could not reach the dashboard server', body: 'Try again.', next: 'retry' }
  switch (e.code) {
    case 'human.click_refused':
      // B11: a sequence (one review, N signatures) that stopped after some signatures: say what landed (core's words)
      // aborted: what landed stays a draft and starting again would make a NEW ticket; resumable: only the terminal finishes it
      if (e.reason === 'stopped') return { title: 'Signing stopped part way', body: `${e.message} Finish it on its page. Starting again makes a new ticket.`, next: 'open' }
      if (e.reason === 'resumable') return { title: 'Not finished yet', body: e.message, next: 'open' }
      return REFUSED[e.reason ?? ''] ?? { title: 'Refused', body: `The signing helper refused this (${e.reason ?? 'no reason'}). Nothing was signed.`, next: 'terminal' }
    case 'sign.convenience_unavailable': {
      const r = refusalOf(e)!
      // The switch moved after the page chose its way: nothing is wrong, the next try follows the host's current answer.
      if (r.reason === 'disabled') return { title: 'Click-to-sign was switched off', body: `${refusalSentence(r)} To sign in the terminal, the key must first move back to a passphrase: "orch keys custody passphrase --workspace <prefix> --in-place". Try again makes a new request for the terminal.`, next: 'retry', stale: true, devices: r.devices }
      return { title: 'Click-to-sign is not available here', body: r.reason === 'dev_host' ? refusalSentence(r) : `${refusalSentence(r)}${OFF}`, next: 'terminal', devices: r.devices }
    }
    case DEV_REFUSED:
      return { title: 'Refused by a dev sandbox host', body: devRefusedSentence(e), next: 'terminal' }
    case 'intent.done':
      return { title: 'Already done', body: alreadySigned(e), next: 'again', signedAt: e.signed_at, details: alreadyDetails(e) }
    case 'addon.already':
      // The add-on is already as asked: plain words, nothing to sign, nothing to "do again".
      return { title: 'Already done', body: e.message, next: 'open' }
    case 'intent.failed':
      return { title: 'The earlier attempt did not land', body: e.message, next: 'prepare' }
    case 'lock.busy':
    case 'host.busy':
      return { title: 'Busy', body: 'The host is busy. Try again shortly.', next: 'retry' }
    case 'intent.expired':
      return { title: 'This attempt is too old; start again', body: e.message, next: 'prepare' }
    case 'intent.blocked':
      return { title: 'The earlier attempt ended', body: e.message, next: 'prepare' }
    case 'intent.open':
      return { title: 'An attempt for this is already open', body: e.message || 'The host holds an open request for this action. Wait until it ends or expires.', next: 'open' }
    case 'sign.convenience_readers': {
      const r = refusalOf(e)!
      // A known reason is said in plain words; without one (a host before H12d-2) the host's own message stays.
      return { title: 'This would add a reader', body: [r.reason ? refusalSentence(r) : e.message, e.hint, typeof e.fix === 'string' ? e.fix : e.fix ? JSON.stringify(e.fix) : null].filter(Boolean).join(' '), next: 'terminal', devices: r.devices }
    }
    case 'reader.decl_invalid':
      return { title: 'The reader declaration did not verify', body: refusalSentence(refusalOf(e)!), next: 'terminal' }
    case 'rate.limited':
      return { title: 'Too many signatures', body: `The signing helper allows only so many signatures per minute and hour. Try again in ${Math.max(1, Math.ceil((e.retry_after_ms ?? 1000) / 1000))} s.`, next: 'retry', waitMs: e.retry_after_ms ?? 1000 }
    case 'custody.no_key':
      return { title: 'No key on this computer', body: 'There is no signing key on this computer. Nothing was signed. Turn One click off and on again, or sign in a terminal.'+OFF, next: 'terminal' }
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
