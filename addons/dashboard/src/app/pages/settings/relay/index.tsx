import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Laptop, Send, Smartphone, Tablet, Upload } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import type { DeviceScope, RelayDevice, RelayLink, RelayQueueItem, RelayState, Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { toastApiError } from '@/app/toast'
import { ago, fmtTime, Mono, Pill, Section } from '../../ticket/shared'
import { PairDialog } from './PairDialog'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'

const LINK: Record<RelayLink, { word: string; dot: string; text: string }> = {
  off: { word: 'Not connected', dot: 'border border-text-faint', text: 'The workspace has never been linked to the relay.' },
  connecting: { word: 'Connecting…', dot: 'bg-warning animate-pulse', text: 'Dialling out to the relay.' },
  online: { word: 'Online', dot: 'bg-success', text: 'Paired devices reach this workspace through the relay.' },
  reconnecting: { word: 'Reconnecting…', dot: 'bg-warning animate-pulse', text: 'The connection dropped. Retrying, 1 s at first and up to 10 s.' },
  stopped: { word: 'Stopped', dot: 'border border-text-faint', text: 'You stopped the link. Devices cannot reach this workspace; this dashboard keeps running.' },
}
const SCOPE: Record<DeviceScope, string> = { look: 'Look', decide: 'Decide', operate: 'Operate', type: 'Type' }
const PLATFORM = { mac: Laptop, linux: Laptop, iphone: Smartphone, ipad: Tablet } as const
const QUEUE_ICON: Record<RelayQueueItem['kind'], typeof Send> = { seal_key: KeyRound, push: Send, drop: Upload, answer: Send }
const day = (iso: string) => {
  const d = new Date(iso)
  return `${d.getUTCDate()} ${d.toLocaleString('en-GB', { month: 'short', timeZone: 'UTC' })} ${d.getUTCFullYear()}`
}

function EpochCell({ d, epoch }: { d: RelayDevice; epoch: number }) {
  if (d.epoch === epoch) return <span>epoch {epoch}</span>
  if (d.epoch === 0) return <span className="text-warning">key on its way</span>
  return <span className="text-warning">epoch {d.epoch} · new key waiting</span>
}

/** Settings > Relay & devices (Preview): the relay link, paired devices, pairing and the sync queue, all simulated. */
export function Relay({ workspace, canEdit, viewer }: { workspace: Workspace; canEdit: boolean; viewer: string }) {
  const qc = useQueryClient()
  const signed = useSignedAction()
  const [pairing, setPairing] = useState(false)
  const [removing, setRemoving] = useState<RelayDevice | null>(null)
  const [stopping, setStopping] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const relay = useQuery({
    queryKey: ['relay', workspace.id],
    queryFn: async () => ({ state: await api.getRelay(workspace.id), at: Date.now() }),
    // Poll while something is moving: the link settling, the queue draining, a pairing code open.
    refetchInterval: (q) => {
      const r = q.state.data?.state
      if (!r) return false
      const moving = r.link === 'connecting' || r.link === 'reconnecting' || (r.link === 'online' && r.queue.some((i) => i.state === 'queued')) || !!r.pairing
      return moving ? 1000 : false
    },
  })
  const r: RelayState | undefined = relay.data?.state

  if (!r)
    return (
      <div className="space-y-4" aria-busy="true">
        <h2 className="text-base font-semibold">Relay &amp; devices</h2>
        <Skeleton className="h-32 w-full" />
      </div>
    )

  const L = LINK[r.link]
  const on = r.link !== 'off' && r.link !== 'stopped'
  const refresh = () => qc.invalidateQueries({ queryKey: ['relay', workspace.id] })
  const startPairing = async () => {
    try {
      await api.postRelay(workspace.id, { op: 'pair.start' })
      await refresh()
      setPairing(true)
    } catch (e) {
      toastApiError(e, 'Could not make a pairing code')
    }
  }
  const remaining = r.devices.filter((d) => !d.this_device && d.id !== removing?.id).length
  const queued = r.queue.filter((i) => i.state === 'queued')
  const sent = r.queue.filter((i) => i.state === 'sent').reverse().slice(0, 5)
  const nameOf = (person: string) => workspace.members.find((m) => m.person === person)?.name ?? person

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">Relay &amp; devices</h2>
        <Pill>Preview</Pill>
        <Pill tone="info">Simulated</Pill>
      </div>
      <p className="text-[13px] text-text-muted">
        Everything on this tab is simulated: no relay is contacted and no key leaves this machine. orch-relay is an API without pages; its settings live here and in the orch app on the iPhone.
      </p>

      <Section
        title="Connection"
        aside={
          canEdit &&
          (on ? (
            <Button variant="outline" size="sm" onClick={() => setStopping(true)}>
              Stop the link
            </Button>
          ) : (
            <Button size="sm" onClick={() => setConnecting(true)}>
              Connect
            </Button>
          ))
        }
      >
        <div className="flex items-start gap-3 text-[13px]">
          <span className={cn('mt-1.5 size-2.5 shrink-0 rounded-full', L.dot)} aria-hidden />
          <div className="min-w-0 flex-1 space-y-0.5">
            <p className="font-medium" role="status">
              {L.word}
              {r.since && r.link !== 'connecting' && <span className="font-normal text-text-muted"> · since {fmtTime(r.since)} UTC</span>}
            </p>
            <p className="text-text-muted">{L.text}</p>
          </div>
        </div>
        <dl className="mt-3 grid grid-cols-[140px_1fr] gap-x-3 gap-y-1.5 text-[13px]">
          <dt className="text-text-muted">Relay</dt>
          <dd>
            <Mono>{r.relay_url}</Mono>
          </dd>
          <dt className="text-text-muted">Services</dt>
          <dd>directory · bridge · drop · push</dd>
          <dt className="text-text-muted">Workspace key</dt>
          <dd>
            epoch {r.epoch} · since {day(r.epoch_started)} · next rotation {day(r.next_rotation)}
          </dd>
        </dl>
        <p className="mt-2 text-[12px] text-text-faint">The key rotates every 90 days and whenever a device is removed. Devices that are offline pick up the new key at their next connect.</p>
        <p className="mt-1 text-[12px] text-text-faint">Who may use this relay (an organisation admin view) comes later, as a dashboard addon (P6).</p>
        {canEdit && r.link === 'online' && (
          <div className="mt-3 flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2">
            <span className="flex-1 text-[12px] text-text-muted">Simulation: see how the page behaves when the network drops.</span>
            <Button
              variant="outline"
              size="sm"
              onClick={async () => {
                try {
                  await api.simulateRelay(workspace.id, { op: 'drop' })
                  await refresh()
                } catch (e) {
                  toastApiError(e, 'Could not simulate a drop')
                }
              }}
            >
              Simulate a dropped connection
            </Button>
          </div>
        )}
      </Section>

      <Section
        title={`Devices (${r.devices.length})`}
        aside={
          canEdit && (
            <span className="flex items-center gap-2">
              {r.link !== 'online' && <span className="text-[12px] text-text-faint">Connect the relay first</span>}
              <Button size="sm" variant="outline" disabled={r.link !== 'online'} onClick={() => void startPairing()}>
                Pair a device
              </Button>
            </span>
          )
        }
        className="[&>div]:p-0"
      >
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Device</TableHead>
              <TableHead>Person</TableHead>
              <TableHead>Scopes</TableHead>
              <TableHead>Key</TableHead>
              <TableHead>Last seen</TableHead>
              {canEdit && <TableHead className="w-24"><span className="sr-only">Actions</span></TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {r.devices.map((d) => {
              const Icon = PLATFORM[d.platform]
              return (
                <TableRow key={d.id} className="text-[13px]">
                  <TableCell>
                    <span className="flex flex-wrap items-center gap-1.5">
                      <Icon className="size-3.5 text-text-muted" aria-hidden />
                      <span className="font-medium">{d.label}</span>
                      {d.this_device && <Pill tone="brand">This device</Pill>}
                      {d.primary && <Pill>Primary</Pill>}
                    </span>
                  </TableCell>
                  <TableCell>{nameOf(d.person)}{d.person === viewer && <span className="text-text-faint"> (you)</span>}</TableCell>
                  <TableCell className="text-text-muted">{d.scopes.map((s) => SCOPE[s]).join(' · ')}</TableCell>
                  <TableCell>
                    <EpochCell d={d} epoch={r.epoch} />
                  </TableCell>
                  <TableCell className="text-text-muted">{d.this_device ? 'now' : d.last_seen ? ago(d.last_seen, Date.parse(r.now)) : 'never'}</TableCell>
                  {canEdit && (
                    <TableCell className="text-right">
                      {!d.this_device && (
                        <Button variant="ghost" size="sm" className="text-danger" onClick={() => setRemoving(d)}>
                          Remove
                        </Button>
                      )}
                    </TableCell>
                  )}
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </Section>

      <Section title="Sync queue" aside={<span className="text-[12px] text-text-faint">{queued.length} waiting</span>}>
        <p className="mb-2 text-[12px] text-text-muted">
          {r.link === 'online' ? 'Sealed items go out one after another.' : 'Sealed items wait here until the relay is online.'} The relay sees sizes and timing, never the content.
        </p>
        {queued.length === 0 && sent.length === 0 ? (
          <p className="text-[13px] text-text-faint">Nothing to send.</p>
        ) : (
          <ul className="divide-y divide-border text-[13px]" aria-label="Sync queue">
            {[...queued, ...sent].map((i) => {
              const Icon = QUEUE_ICON[i.kind]
              return (
                <li key={i.id} className="flex items-center gap-2 py-1.5">
                  <Icon className="size-3.5 shrink-0 text-text-muted" aria-hidden />
                  <span className="min-w-0 flex-1 truncate">{i.label}</span>
                  {i.state === 'queued' ? <Pill tone="warning">Queued</Pill> : <Pill tone="success">Sent {fmtTime(i.sent_at!).slice(-5)}</Pill>}
                  <span className="w-28 text-right text-[12px] text-text-faint">{ago(i.queued_at, Date.parse(r.now))}</span>
                </li>
              )
            })}
          </ul>
        )}
      </Section>

      {connecting && (
        <SignPrompt
          title={`Connect ${workspace.name} to the relay`}
          covers={[`Relay: ${r.relay_url}`, 'The host dials out; nothing listens on the internet', 'Paired devices can reach this workspace, each within its scopes']}
          confirmLabel="Sign and connect"
          onClose={() => setConnecting(false)}
          onSign={() => {
            setConnecting(false)
            void signed('Relay link on', () => api.postRelay(workspace.id, { op: 'connect' }))
          }}
        />
      )}
      {pairing && relay.data && <PairDialog workspace={workspace} relay={r} fetchedAt={relay.data.at} onClose={() => setPairing(false)} />}
      {stopping && (
        <SignPrompt
          title="Stop the relay link"
          destructive
          covers={['Open streams end and nothing more runs from a device', 'Paired devices stay paired; Connect brings them back', 'This dashboard keeps running on this machine']}
          confirmLabel="Sign and stop"
          onClose={() => setStopping(false)}
          onSign={() => {
            setStopping(false)
            void signed('Relay link stopped', () => api.postRelay(workspace.id, { op: 'stop' }))
          }}
        />
      )}
      {removing && (
        <SignPrompt
          title={`Remove ${removing.label} from ${workspace.name}`}
          destructive
          covers={[
            `Device: ${removing.label} (${nameOf(removing.person)})`,
            `Starts epoch ${r.epoch + 1}: the new key is sealed to the ${remaining === 1 ? 'remaining device' : `${remaining} remaining devices`}`,
            'It keeps what it already downloaded; it gets nothing new',
          ]}
          confirmLabel="Sign and remove"
          onClose={() => setRemoving(null)}
          onSign={() => {
            const d = removing
            setRemoving(null)
            void signed(`Removed ${d.label}`, () => api.postRelay(workspace.id, { op: 'device.remove', device: d.id }))
          }}
        />
      )}
    </div>
  )
}
