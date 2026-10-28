// The devices a click-to-sign refusal names (H12d-2 `Error.detail.devices`): by their label and member where this page
// knows them, never by raw id. Labels are the device's own words (untrusted): escaped and isolated (visible.tsx).
import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import type { RosterMember } from '@/api/settings'
import { PREFIX_PLACEHOLDER, withPrefix } from '@/api/click'
import { Inline, Raw } from './visible'

export interface NamedDevice {
  id: string
  /** The device's label (untrusted), or null. */
  label: string | null
  /** The member's display name, or null when the device is not in the member list. */
  member: string | null
}

/** Each id with what the roster says about it. Pure (tests). */
export function nameDevices(ids: readonly string[], roster: readonly RosterMember[] | undefined): NamedDevice[] {
  return ids.map((id) => {
    const owner = roster?.find((m) => m.devices.some((d) => d.id === id))
    const device = owner?.devices.find((d) => d.id === id)
    return { id, label: typeof device?.label === 'string' && device.label ? device.label : null, member: owner?.name ?? null }
  })
}

export function DeviceName({ device }: { device: NamedDevice }) {
  if (device.label)
    return (
      <span data-testid="refusal-device">
        {/* One line, in its own box: an LF or quotes in the label can't fake a second entry (review L2). */}
        <span data-testid="device-label-box" className="rounded bg-surface-3 px-1 font-mono text-[12px] text-text">
          <Inline>{device.label}</Inline>
        </span>
        <span className="text-text-muted"> (named by the device)</span>
        {device.member && (
          <>
            {' '}of <Inline>{device.member}</Inline>
          </>
        )}
      </span>
    )
  if (device.member)
    return (
      <span data-testid="refusal-device">
        <Inline>{device.member}</Inline>’s device (unnamed)
      </span>
    )
  return <span data-testid="refusal-device">a device this page does not know</span>
}

/** "Devices: …" under a refusal sentence; nothing when the host named none. */
export function RefusalDevices({ ws, ids }: { ws: string; ids: readonly string[] }) {
  const roster = useQuery({ ...queries.roster(ws), enabled: ids.length > 0 })
  if (ids.length === 0) return null
  const named = nameDevices(ids, roster.data)
  return (
    <p data-testid="refusal-devices" className="text-[13px] text-text-muted">
      {ids.length === 1 ? 'The device: ' : `The devices (${ids.length}): `}
      {named.map((d, i) => (
        <span key={d.id}>
          {i > 0 && '; '}
          <DeviceName device={d} />
        </span>
      ))}
      .
    </p>
  )
}

/** Raw host text (ids, hashes) folded away: a person never has to read it. */
export function TechnicalDetails({ text }: { text: string }) {
  return (
    <details data-testid="technical-details" className="mt-1 text-[12px] text-text-muted">
      <summary className="cursor-pointer">Technical details</summary>
      <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono">
        <Raw>{text}</Raw>
      </pre>
    </details>
  )
}

/** A click-to-sign problem as the sign panel and the hand-off dialog show it: title, sentence, the devices it names. */
export function ClickProblemBox({ problem, ws }: { problem: { title: string; body: string; next?: string; signedAt?: string | null; details?: string; devices?: string[] }; ws: string | undefined }) {
  // The sentences name the workspace as a placeholder: the real prefix goes in here, where the workspace is known.
  const needsPrefix = problem.body.includes(PREFIX_PLACEHOLDER)
  const workspaces = useQuery({ ...queries.workspacesAll(), enabled: needsPrefix })
  const prefix = workspaces.data?.find((w) => w.id === ws || w.prefix === ws)?.prefix
  const body = needsPrefix ? withPrefix(problem.body, prefix) : problem.body
  return (
    <div role="alert" data-testid="click-problem" data-click-next={problem.next} className="rounded-md border border-warning/40 px-3 py-2 text-[13px]">
      <p className="font-medium text-text">{problem.title}</p>
      {body && (
        <p title={problem.signedAt ?? undefined} className="text-text-muted">
          <Raw>{body}</Raw>
        </p>
      )}
      {problem.details && <TechnicalDetails text={problem.details} />}
      {ws && problem.devices && <RefusalDevices ws={ws} ids={problem.devices} />}
    </div>
  )
}
