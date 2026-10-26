// Settings → Devices, "Reader levels" (core H12d-2): every current device of the workspace with the reader level the
// host counts now, and the workspace's `required_reader`. A device below the required level holds click-to-sign back
// (or cannot be added): it needs an update of the orch app, which then declares what it reads. Read only: a declaration
// comes from the device itself (`orch keys reader declare`), never from this page.
import { useQuery } from '@tanstack/react-query'
import { hasFeature } from '@/api/features'
import { queries } from '@/api/queries'
import type { HostReader, RosterMember } from '@/api/settings'
import { DeviceName, nameDevices } from '@/components/sign/RefusalDevices'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Link } from '@tanstack/react-router'
import { api } from '@/api/client'
import { Pill } from '../../ticket/shared'

export const NEEDS_UPDATE = 'Needs an update of the orch app'

/** Why the workspace requires a level, in plain words. */
export const requiredLine = (required: number | null): string =>
  required === null
    ? 'The host does not say which reader level this workspace requires.'
    : required >= 1
      ? `Every device must read at level ${required} or higher: click-to-sign is on for this workspace, or it has click-signed entries. A device below it holds click-to-sign back and cannot be added.`
      : 'No reader level is required: click-to-sign is off and has not been used in this workspace.'

export interface ReaderRow {
  reader: HostReader
  below: boolean
}

/** The rows in a stable order: devices below the required level first, then by member and device. Pure (tests). */
export function readerRows(readers: readonly HostReader[], required: number | null, roster: readonly RosterMember[] | undefined): ReaderRow[] {
  const name = (r: HostReader) => roster?.find((m) => m.person === r.person)?.name ?? r.person
  return readers
    .map((reader) => ({ reader, below: required !== null && reader.level < required }))
    .sort((a, b) => Number(b.below) - Number(a.below) || name(a.reader).localeCompare(name(b.reader)) || a.reader.device.localeCompare(b.reader.device))
}

export function ReaderLevels({ ws }: { ws: string }) {
  const served = hasFeature('readers', ws)
  const readers = useQuery({ ...queries.readers(ws), enabled: served })
  const roster = useQuery({ ...queries.roster(ws), enabled: served })
  const click = useQuery({ queryKey: ['click-status', ws], queryFn: () => api.clickStatus(ws), enabled: served })
  const mine = click.data?.device
  return (
    <section className="space-y-3" aria-labelledby="readers-heading" data-testid="reader-levels">
      <h2 id="readers-heading" className="text-base font-semibold">
        Phone and devices
      </h2>
      {!served || readers.data?.available === false ? (
        <p data-testid="readers-unavailable" className="text-[13px] text-text-muted">
          This host does not report reader levels (it runs an orch-core from before click-to-sign readers).
        </p>
      ) : readers.isError ? (
        <p role="alert" className="text-[13px] text-text-muted">
          The reader levels could not be loaded.
        </p>
      ) : !readers.data ? (
        <p className="text-[13px] text-text-muted">Loading…</p>
      ) : (
        <ReaderPanel readers={readers.data.readers} required={readers.data.required_reader} roster={roster.data} mine={mine} />
      )}
    </section>
  )
}

/** This computer is the signer: it is shown as ready and never counted (the host's gate skips it too). With no other
 * device the table and its warning do not appear at all: "Only this computer approves for now" + Connect a hub. */
function ReaderPanel({ readers, required, roster, mine }: { readers: HostReader[]; required: number | null; roster: RosterMember[] | undefined; mine: string | undefined }) {
  const others = readers.filter((r) => r.device !== mine)
  return (
    <div className="space-y-3">
      <div data-testid="this-computer" className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-[13px]">
        <span>
          <b>This computer</b>
          <span className="block text-[11px] text-text-muted">Approves with one click</span>
        </span>
        <Pill tone="success">Ready</Pill>
      </div>
      {others.length === 0 ? (
        <div data-testid="only-this-computer" className="space-y-2 rounded-md border border-border px-3 py-3 text-[13px]">
          <b>Only this computer approves for now</b>
          <p className="text-text-muted">To add a phone or another computer, connect a hub first.</p>
          <Link data-testid="readers-connect-hub" to="/settings/$tab" params={{ tab: 'hub' }} className="inline-block rounded-md border border-border px-3 py-1 text-chart-4 underline">
            Connect a hub
          </Link>
        </div>
      ) : (
        <ReaderTable readers={others} required={required} roster={roster} />
      )}
    </div>
  )
}

function ReaderTable({ readers, required, roster }: { readers: HostReader[]; required: number | null; roster: RosterMember[] | undefined }) {
  const rows = readerRows(readers, required, roster)
  const below = rows.filter((r) => r.below).length
  return (
    <div className="space-y-3">
      <p data-testid="readers-required" data-required={required ?? ''} className="text-[13px] text-text-muted">
        {requiredLine(required)}
      </p>
      {below > 0 && (
        <p role="status" data-testid="readers-below" className="rounded-md border border-warning/40 px-3 py-2 text-[13px] text-warning">
          {below === 1 ? 'One device is' : `${below} devices are`} below the required level. Update the orch app on {below === 1 ? 'it' : 'them'} (it then declares what it reads), or remove {below === 1 ? 'it' : 'them'}.
        </p>
      )}
      {rows.length === 0 ? (
        <p className="text-[13px] text-text-muted">The host lists no device.</p>
      ) : (
        <div className="overflow-x-auto" data-scroll-x>
          <Table aria-label="Reader levels">
            <TableHeader>
              <TableRow>
                <TableHead>Device</TableHead>
                <TableHead>Level</TableHead>
                <TableHead>State</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map(({ reader, below: low }) => {
                const [named] = nameDevices([reader.device], roster)
                return (
                  <TableRow key={reader.device} data-testid="reader-row" data-below={low ? 'true' : 'false'} className="align-top">
                    <TableCell className="max-w-80 whitespace-normal">
                      <DeviceName device={named} />
                    </TableCell>
                    <TableCell data-testid="reader-level">
                      Level {reader.level}
                      <span className="block text-[11px] text-text-muted">{reader.level >= 1 ? 'reads click-signed entries' : required !== null && required >= 1 ? 'this app needs an update to read entries signed with one click' : 'does not read click-signed entries; nothing here uses one-click signing yet'}</span>
                      {reader.reader !== reader.level && <span className="text-text-muted"> (declared {reader.reader})</span>}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      {low ? (
                        <Pill tone="warning">{NEEDS_UPDATE}</Pill>
                      ) : (
                        <span className="text-text-muted">{required !== null && required >= 1 ? 'Can read click-signed entries' : 'Nothing needed now'}</span>
                      )}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}
