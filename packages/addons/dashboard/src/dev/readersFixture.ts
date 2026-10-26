// DEV/TEST ONLY: core H12d-2's reads as the dashboard server forwards them (`/members` with the read_ext/3 device fields,
// `/readers` from `reader.list`), and every click-to-sign refusal the page explains. The layout preview and the tests
// answer the api with these; nothing here reaches a host. Labels are the devices' own (untrusted) words.
import { ApiError } from '@/api/types'
import type { HostReaders, RosterMember, RosterDevice } from '@/api/settings'
import { DECL_INVALID, READERS_REASONS, UNAVAILABLE_REASONS } from '@/api/refusals'

export const ME = 'p_' + 'a'.repeat(32)
export const MARA = 'p_' + 'b'.repeat(32)
export const dev = (c: string) => 'd_' + c.repeat(32)
export const MAC = dev('f')
export const PHONE = dev('1')
export const LAPTOP = dev('2')
export const OLD_PHONE = dev('3')
const LONG = `Severin’s iPhone 17 Pro Max ${'U'.repeat(90)}`.slice(0, 80)

const device = (id: string, over: Partial<RosterDevice> = {}): RosterDevice => ({
  id, auth: null, scopes: ['look', 'decide'], valid: true, label: null, label_hidden: false, paired_at: null, relay: false, cert_expires_at: null, reader: 1, ...over,
})

/** `/members` in the real shape (H12d-2): every device carries the read_ext/3 fields and `reader`. */
export function readersRoster(): RosterMember[] {
  return [
    {
      person: ME, name: 'Severin', role: 'owner', devices: [
        device(MAC, { auth: 'passphrase', scopes: ['look', 'decide', 'operate', 'type'] }),
        device(PHONE, { label: LONG, relay: true, paired_at: '2026-10-12T09:00:00Z', cert_expires_at: '2027-10-12T09:00:00Z' }),
        device(OLD_PHONE, { label: 'Work phone\nsecond line', relay: true, paired_at: '2026-10-01T09:00:00Z', reader: null }),
      ],
    },
    { person: MARA, name: 'Mara', role: 'maintainer', devices: [device(LAPTOP, { reader: 0 }), ...Array.from({ length: 6 }, (_, i) => device(dev(String.fromCharCode(97 + i)), { label: i % 2 ? `Mara’s tablet ${i}` : null, reader: i % 3 ? 1 : 0 }))] },
  ]
}

/** `/readers`: every current device of the roster with its counted level, and `required_reader`. */
export function readersOf(roster: RosterMember[], required: number): HostReaders {
  return {
    available: true,
    required_reader: required,
    readers: roster.flatMap((m) => m.devices.map((d) => ({ device: d.id, person: m.person, reader: d.reader ?? 0, level: d.reader ?? 0 }))),
  }
}

export interface RefusalSample {
  title: string
  error: ApiError
}

/** Every refusal of the two convenience codes (each reason, with devices), `reader.decl_invalid`, and an unknown reason. */
export function refusalSamples(): RefusalSample[] {
  const err = (code: string, detail: Record<string, unknown> | undefined, message = `${code}: as the host says it, with a long tail ${'U'.repeat(90)}`) =>
    new ApiError(409, { code, message, retryable: false, ...(detail ? { detail } : {}) })
  return [
    ...UNAVAILABLE_REASONS.map((r) => ({ title: `sign.convenience_unavailable · ${r}`, error: err('sign.convenience_unavailable', { reason: r, devices: r === 'other_member' ? [LAPTOP, dev('9')] : r === 'phone_device' ? [OLD_PHONE, PHONE] : [] }) })),
    ...READERS_REASONS.map((r) => ({ title: `sign.convenience_readers · ${r}`, error: err('sign.convenience_readers', { reason: r, devices: [OLD_PHONE] }) })),
    { title: 'sign.convenience_readers · a reason this page does not know', error: err('sign.convenience_readers', { reason: 'relay_reader_old', devices: [] }) },
    { title: DECL_INVALID, error: err(DECL_INVALID, undefined, 'signature: the declaration does not verify') },
  ]
}
