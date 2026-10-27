// Core H12d-2: why click-to-sign is refused, in plain sentences (host contract section 0.8.6 items 1 and 9, section 0.3
// `Error.detail`). The host sends a closed `reason` and the devices concerned (`detail.devices`, device ids only); the
// dashboard server drops every other key and every reason not listed here (server errors.py). The page checks again,
// names the devices by their label where it knows them (never by raw id), and shows nothing it does not know.
import { ApiError } from './types'

export const UNAVAILABLE_REASONS = ['idle', 'disabled', 'relay_paired', 'other_member', 'phone_device', 'reader_version', 'pair_device', 'dev_host'] as const
export const READERS_REASONS = ['no_declaration', 'reader_too_old', 'declaration_unknown', 'relay_too_old', 'hub_too_old', 'unknown_readers'] as const
export type UnavailableReason = (typeof UNAVAILABLE_REASONS)[number]
export type ReadersReason = (typeof READERS_REASONS)[number]

export const CONVENIENCE_UNAVAILABLE = 'sign.convenience_unavailable'
export const CONVENIENCE_READERS = 'sign.convenience_readers'
export const DECL_INVALID = 'reader.decl_invalid'

/** Signing from the dashboard on a dev sandbox host (core D1, #582): the one text, shared by the refusal and the signing card. */
export const DEV_HOST_SENTENCE =
  'Signing from the dashboard is off on a dev sandbox host. This host was started with --dev-sim-convenience for the Simulator test pair, which accepts click-signed answers only from the throwaway sandbox workspace, so it never uses this computer’s key store for your own signatures (and refuses it when HOME is inside a sandbox folder). One click is unavailable on this host.'

/** `sign.convenience_unavailable`: the host refuses a click-signed (convenience) signature, first match wins. */
export const UNAVAILABLE_SENTENCE: Record<UnavailableReason, string> = {
  idle: 'One click switched itself off after a while without use. Turn it on again to continue.',
  disabled: 'Click-to-sign is switched off for this workspace.',
  relay_paired:
    'Click-to-sign is refused because the relay’s side of this workspace has not declared that it reads click-signed entries. Update the relay.',
  other_member:
    'This workspace has another member who cannot yet read click-signed entries (their orch, or one of their devices). Click-to-sign stays off until that member updates orch and declares what it reads, on their computer and on each of their devices; a removed device no longer counts.',
  phone_device:
    'You have another device in this workspace whose orch app cannot yet read click-signed entries. Update orch on it (it then declares what it reads), or remove it.',
  reader_version: 'The host’s orch-core does not accept click-signed entries. Update orch-core on this computer.',
  pair_device:
    'One click is not ready for adding this phone.',
  dev_host: DEV_HOST_SENTENCE,
}

/** `sign.convenience_readers`: adding a reader is refused while click-to-sign is on or the history has click-signed entries. */
export const READERS_SENTENCE: Record<ReadersReason, string> = {
  no_declaration:
    'The new device did not say which entries its orch app can read (an app older than click-to-sign, or added without its reader declaration). It cannot join a workspace that uses click-to-sign: update its orch app and add it again with its declaration.',
  reader_too_old:
    'A device’s orch app is too old to show what click-to-sign changes: update it.',
  declaration_unknown:
    'The host does not hold this device’s current reader declaration (the host’s records were lost or rolled back, or the app was downgraded). Declare again from the device, then try again.',
  relay_too_old:
    'The relay is too old to carry click-signed entries, so this workspace cannot be linked to it while click-to-sign is on or has been used here.',
  hub_too_old:
    'This hub is too old to read click-signed entries, so this workspace cannot join it while click-to-sign is on or has been used here. Update the hub, or switch click-to-sign off first.',
  unknown_readers:
    'This would hand the workspace’s records to readers it does not know, while click-to-sign is on or has been used here, and they may not read click-signed entries. It was refused.',
}

/** `reader.decl_invalid`: a reader declaration that does not verify (it can appear when a device is added or paired). */
export const DECL_INVALID_SENTENCE =
  'The device’s reader declaration did not verify (it is for another workspace or device, out of date, or damaged). Update the reader declaration on that device.'

/** The click signer's per-workspace `problem` (status card), for the values it names. */
export const SIGNER_PROBLEM_SENTENCE: Record<string, string> = {
  idle: 'One click switched itself off after a while without use. Turn it on again to continue.',
  readers_old:
    'Click-to-sign is paused: another device or member of this workspace has an orch app too old to read click-signed entries. Update orch there (it then declares what it reads) or remove that device; Settings → Devices shows which. ',
  disabled: 'Click-to-sign is switched off for this workspace.',
  dev_host: DEV_HOST_SENTENCE,
  not_convenience: 'Your key for this workspace is not held for click-to-sign.',
  version_mismatch: 'The signing helper and the host are different versions of orch-core. Update orch-core and restart the dashboard.',
  custody_unavailable: 'The key store is locked or refused access.',
}

/** The signer's problem as a sentence; an unnamed one stays the signer's own short text (shown as text). */
export const signerProblem = (problem: string | null | undefined): string | null =>
  problem ? (SIGNER_PROBLEM_SENTENCE[problem] ?? `The signing helper says: ${problem}.`) : null

const DEVICE = /^d_[0-9a-f]{32}$/
const has = <T extends string>(list: readonly T[], v: unknown): v is T => typeof v === 'string' && (list as readonly string[]).includes(v)

export interface Refusal {
  code: typeof CONVENIENCE_UNAVAILABLE | typeof CONVENIENCE_READERS | typeof DECL_INVALID
  /** From the closed set of the code, or null (unknown reasons are never shown). */
  reason: string | null
  /** Device ids the refusal is about; [] when the host named none. */
  devices: string[]
}

/**
 * The refusal of an error of the three codes, or null. The reason comes from `detail.reason`, else the top-level
 * `reason` (the click signer's channel carries it there); only members of the code's set count. `devices` must be a
 * list of device ids, or none is taken.
 */
export function refusalOf(e: unknown): Refusal | null {
  if (!(e instanceof ApiError)) return null
  if (e.code === DECL_INVALID) return { code: DECL_INVALID, reason: null, devices: [] }
  const list = e.code === CONVENIENCE_UNAVAILABLE ? UNAVAILABLE_REASONS : e.code === CONVENIENCE_READERS ? READERS_REASONS : null
  if (!list) return null
  const detail = e.detail && typeof e.detail === 'object' ? e.detail : null
  const raw = detail?.reason ?? e.reason
  const devices = Array.isArray(detail?.devices) && detail.devices.length <= 64 && detail.devices.every((d) => typeof d === 'string' && DEVICE.test(d)) ? [...new Set(detail.devices)] : []
  return { code: e.code as Refusal['code'], reason: has(list, raw) ? raw : null, devices }
}

/** The plain sentence of a refusal. A reason the page does not know gets the code's general sentence. */
export function refusalSentence(r: Refusal): string {
  if (r.code === DECL_INVALID) return DECL_INVALID_SENTENCE
  if (r.code === CONVENIENCE_UNAVAILABLE)
    return r.reason ? UNAVAILABLE_SENTENCE[r.reason as UnavailableReason] : 'The host does not allow click-to-sign for this workspace now.'
  return r.reason
    ? READERS_SENTENCE[r.reason as ReadersReason]
    : 'This would add a reader to a workspace that uses click-to-sign, and the host refused it until that reader can read click-signed entries.'
}

// Core D1 (#582): `host.dev_refused`, the dev gate's closed `detail.reason`. Each reason is said in plain words; a reason this
// page does not know gets the generic sentence and is never shown as text.
export const DEV_REFUSED = 'host.dev_refused'
export const DEV_REFUSED_SENTENCE: Record<string, string> = {
  sandbox_workspace: 'This is a throwaway dev-pair sandbox workspace, and this is a normal host. A normal host never serves one. Start a real workspace instead.',
  not_sandbox: 'This is a dev sandbox host: it serves only the sandbox workspace of its test pair, never a real workspace.',
  dev_origin: 'This is a dev sandbox host: it links only to the relay its sandbox was made for, not to another one.',
  dev_host: DEV_HOST_SENTENCE,
  not_foreground: 'The dev flag works only when the host runs in the foreground of a terminal.',
  service_managed: 'The dev flag is refused under a service manager: a dev host is always started by hand.',
  service_dir: 'The dev flag is refused for a service run or state folder: a dev host uses only its own sandbox folder.',
  no_manifest: 'The dev sandbox folder has no valid sandbox manifest.',
  state_not_in_root: 'The host’s state folder is not inside the dev sandbox folder.',
  home_not_in_root: 'The host’s home folder is not inside the dev sandbox folder.',
  no_dev_json: 'The dev sandbox has no dev settings file.',
  dev_json_mismatch: 'The dev settings file does not match the sandbox manifest.',
  no_person: 'The sandbox manifest names no sandbox person.',
  custody_convenience: 'A key of this person is already held for click-to-sign, which a dev sandbox does not allow.',
}
export const DEV_REFUSED_GENERIC = 'The host refused this because it is a dev sandbox host, or because it is not one. Nothing changed.'

/** The reason of a `host.dev_refused` error when it is one the page knows, else null (read from `detail.reason`, else `reason`). */
export function devRefusedReason(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.code !== DEV_REFUSED) return null
  const detail = e.detail && typeof e.detail === 'object' ? e.detail : null
  const raw = detail?.reason ?? e.reason
  return typeof raw === 'string' && Object.hasOwn(DEV_REFUSED_SENTENCE, raw) ? raw : null
}

/** The plain sentence of a `host.dev_refused` error. */
export const devRefusedSentence = (e: unknown): string => {
  const r = devRefusedReason(e)
  return r ? DEV_REFUSED_SENTENCE[r] : DEV_REFUSED_GENERIC
}
