// Settings > Devices > Remote access (R2 T4, orch-core #1590; design note r2-host-design.md section 9/10).
// "Remote access: Off" + "Turn on". Turning on asks ONE question ("Does this computer run at a customer's place?"), a
// level (Terminals apart, never the default) and when it ends, then it is signed with one click and the backup key
// (HubStep, `remote_set`). Devices are listed with Remove (one click). "Add a device" shows a one-time code; the device
// that proves the code appears as "Add device <name>, fingerprint <words>?" and is added with the same signing step.
// Nothing here is typed except the backup key. Ids stay behind the small "i".
import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { LEVEL_HELP, LEVEL_NAME, LEVEL_RANK, remoteQueries, untilText, type Premises, type RemoteCode, type RemoteLevel, type RemoteView } from '@/api/remote'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Button } from '@/components/ui/button'
import { toastApiError } from '@/app/toast'
import { Raw } from '@/components/sign/visible'
import { HubStep } from './HubStep'
import { useOneClick } from '../hub/shared'

const LEVELS: RemoteLevel[] = ['view', 'work', 'terminals']
const DAYS = [1, 3, 7]
const DEVICE_DAYS: { days: number; label: string }[] = [{ days: 30, label: '30 days' }, { days: 90, label: '90 days' }, { days: 365, label: '1 year' }]

function Choice({ name, value, checked, onChange, children, testId }: { name: string; value: string; checked: boolean; onChange: () => void; children: ReactNode; testId: string }) {
  return (
    <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2 text-[13px] has-[:checked]:border-primary has-[:checked]:bg-surface-2">
      <input type="radio" name={name} value={value} checked={checked} onChange={onChange} data-testid={testId} className="mt-0.5" />
      <span className="min-w-0">{children}</span>
    </label>
  )
}

/** The question and the three choices that become one `remote.set`. */
function TurnOn({ ws, view, onClose }: { ws: string; view: RemoteView; onClose: () => void }) {
  const qc = useQueryClient()
  const oneClick = useOneClick(ws)
  const [premises, setPremises] = useState<Premises | null>(null)
  const [level, setLevel] = useState<RemoteLevel>('view')
  const [days, setDays] = useState<number | null>(3)
  const [signing, setSigning] = useState(false)
  const foreverAllowed = premises === 'no' && level !== 'terminals'
  const endDays = !foreverAllowed && days === null ? 3 : days
  const hubReads = view.hub_mode === 'full' && premises === 'yes'
  const ready = premises !== null && !hubReads
  if (signing && premises) {
    return (
      <HubStep
        ws={ws}
        action={{ action: 'remote_set', level, premises, ...(endDays === null ? {} : { days: endDays }), ...(view.isolated === false ? { agents_risk: true } : {}) }}
        onClose={() => { setSigning(false); onClose() }}
        onChanged={() => { void qc.invalidateQueries({ queryKey: ['remote', ws] }) }}
        doneText="Remote access is on. Add a device to use it."
      />
    )
  }
  return (
    <div className="space-y-4" data-testid="remote-turn-on">
      <fieldset className="space-y-2">
        <legend className="text-[13px] font-medium">Does this computer run at a customer&apos;s place?</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          <Choice name="remote-premises" value="no" checked={premises === 'no'} onChange={() => setPremises('no')} testId="remote-premises-no">No</Choice>
          <Choice name="remote-premises" value="yes" checked={premises === 'yes'} onChange={() => setPremises('yes')} testId="remote-premises-yes">Yes</Choice>
        </div>
        {hubReads && <p role="alert" data-testid="remote-hub-full" className="text-[13px] text-warning">The hub reads this workspace, so remote access cannot be on at a customer&apos;s place.</p>}
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-[13px] font-medium">What may a remote device do?</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {LEVELS.filter(l => l !== 'terminals').map(l => (
            <Choice key={l} name="remote-level" value={l} checked={level === l} onChange={() => setLevel(l)} testId={`remote-level-${l}`}>
              <span className="font-medium">{LEVEL_NAME[l]}</span><span className="block text-text-muted">{LEVEL_HELP[l]}</span>
            </Choice>
          ))}
        </div>
        <div className="border-t border-border pt-2">
          <Choice name="remote-level" value="terminals" checked={level === 'terminals'} onChange={() => setLevel('terminals')} testId="remote-level-terminals">
            <span className="font-medium">{LEVEL_NAME.terminals}</span><span className="block text-text-muted">{LEVEL_HELP.terminals}</span>
          </Choice>
        </div>
      </fieldset>
      <fieldset className="space-y-2">
        <legend className="text-[13px] font-medium">When does it end?</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {DAYS.map(d => (
            <Choice key={d} name="remote-days" value={String(d)} checked={endDays === d} onChange={() => setDays(d)} testId={`remote-days-${d}`}>After {d} {d === 1 ? 'day' : 'days'}</Choice>
          ))}
          {foreverAllowed && <Choice name="remote-days" value="forever" checked={endDays === null} onChange={() => setDays(null)} testId="remote-days-forever">Until I turn it off</Choice>}
        </div>
      </fieldset>
      {oneClick !== 'on' && <p data-testid="remote-no-one-click" className="text-[13px] text-text-muted">{oneClick === 'checking' ? 'Checking One click…' : 'One click is not ready, so remote access cannot be signed here yet.'}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="outline" data-testid="remote-cancel" onClick={onClose}>Cancel</Button>
        <Button data-testid="remote-sign" disabled={!ready || oneClick !== 'on'} onClick={() => setSigning(true)}>Continue</Button>
      </div>
    </div>
  )
}

/** The off state: the one button, folded behind "More" with the agents line while agents can act as the person. */
function OffActions({ view, onTurnOn }: { view: RemoteView; onTurnOn: () => void }) {
  const [more, setMore] = useState(false)
  if (view.isolated !== false) return <Button size="sm" data-testid="remote-turn-on-btn" onClick={onTurnOn}>Turn on</Button>
  return (
    <div className="space-y-2">
      <button type="button" data-testid="remote-more" aria-expanded={more} className="text-[13px] text-text-muted underline" onClick={() => setMore(v => !v)}>{more ? 'Less' : 'More'}</button>
      {more && (
        <div className="space-y-2 rounded-md border border-border p-3" data-testid="remote-more-body">
          <p className="text-[13px]">Agents on this computer can act as you</p>
          <Button size="sm" data-testid="remote-turn-on-btn" onClick={onTurnOn}>Turn on</Button>
        </div>
      )}
    </div>
  )
}

function CodeBox({ code }: { code: RemoteCode }) {
  const [copied, setCopied] = useState(false)
  const copy = () => { void navigator.clipboard.writeText(code.code).then(() => setCopied(true), () => setCopied(false)) }
  return (
    <div className="space-y-2" data-testid="remote-code-box">
      <p className="text-[13px]">Type this code on the new device. It works once, for 15 minutes.</p>
      <code data-testid="remote-code" className="block break-all rounded-md border border-border bg-surface-2 p-2 text-[12px]">{code.code}</code>
      <Button size="sm" variant="outline" data-testid="remote-code-copy" onClick={copy}>{copied ? 'Copied' : 'Copy'}</Button>
    </div>
  )
}

/** "Add device <name>, fingerprint <words>?" with a level and an end date, signed with the backup key. */
function PendingRow({ ws, view, row, onDone }: { ws: string; view: RemoteView; row: RemoteView['pending'][number]; onDone: () => void }) {
  const [level, setLevel] = useState<RemoteLevel>('view')
  const [days, setDays] = useState(30)
  const [signing, setSigning] = useState(false)
  const allowed = LEVELS.filter(l => view.level === null || LEVEL_RANK[l] <= LEVEL_RANK[view.level])
  if (signing) {
    return <HubStep ws={ws} action={{ action: 'remote_device_confirm', pending: row.pending, level_cap: level, days }} onClose={() => { setSigning(false); onDone() }} onChanged={onDone} doneText="The device is added." />
  }
  return (
    <div className="space-y-3 rounded-md border border-border p-3" data-testid="remote-pending">
      <p className="text-[13px]">Add device <strong><Raw>{row.name}</Raw></strong>, fingerprint <strong data-testid="remote-pending-words"><Raw>{row.words}</Raw></strong>?</p>
      <p className="text-[12px] text-text-muted">Check that the same words are on the new device.</p>
      <div className="flex flex-wrap gap-2">
        {allowed.filter(l => l !== 'terminals').map(l => (
          <Choice key={l} name={`pending-level-${row.pending}`} value={l} checked={level === l} onChange={() => setLevel(l)} testId={`remote-pending-level-${l}`}>{LEVEL_NAME[l]}</Choice>
        ))}
        {allowed.includes('terminals') && <Choice name={`pending-level-${row.pending}`} value="terminals" checked={level === 'terminals'} onChange={() => setLevel('terminals')} testId="remote-pending-level-terminals">{LEVEL_NAME.terminals}</Choice>}
      </div>
      <div className="flex flex-wrap gap-2">
        {DEVICE_DAYS.map(d => (
          <Choice key={d.days} name={`pending-days-${row.pending}`} value={String(d.days)} checked={days === d.days} onChange={() => setDays(d.days)} testId={`remote-pending-days-${d.days}`}>{d.label}</Choice>
        ))}
      </div>
      <div className="flex justify-end"><Button size="sm" data-testid="remote-pending-add" onClick={() => setSigning(true)}>Add device</Button></div>
    </div>
  )
}

export function RemoteAccess({ ws }: { ws: string }) {
  const qc = useQueryClient()
  const [turning, setTurning] = useState(false)
  const [adding, setAdding] = useState(false)
  const [code, setCode] = useState<RemoteCode | null>(null)
  const status = useQuery(remoteQueries.status(ws, adding))
  const refresh = () => { void qc.invalidateQueries({ queryKey: ['remote', ws] }) }
  const off = useMutation({ mutationFn: () => api.remoteOff(ws), onSuccess: () => { setAdding(false); setCode(null); refresh() }, onError: e => toastApiError(e, "Couldn't turn remote access off.") })
  const remove = useMutation({ mutationFn: (id: string) => api.removeRemoteDevice(ws, id), onSuccess: refresh, onError: e => toastApiError(e, "Couldn't remove the device.") })
  const issue = useMutation({ mutationFn: () => api.remoteCode(ws), onSuccess: c => { setCode(c); setAdding(true) }, onError: e => toastApiError(e, "Couldn't make a code.") })
  const view = status.data
  if (status.isPending && status.fetchStatus !== 'idle') return null
  if (status.isError || !view) return <p data-testid="remote-error" className="text-[13px] text-text-muted">Remote access is not available right now.</p>
  const waiting = view.pending
  return (
    <section aria-label="Remote access" data-testid="remote-access" className="space-y-4">
      <h2 className="text-base font-semibold">Remote access</h2>
      <div className="flex flex-wrap items-center gap-3">
        <p data-testid="remote-state" className="text-[13px]">
          Remote access: <strong>{view.on ? 'On' : 'Off'}</strong>
          {view.on && view.level && <span className="text-text-muted"> ({LEVEL_NAME[view.level]}, {untilText(view.until_ms)})</span>}
        </p>
        {view.on && <Button size="sm" variant="outline" data-testid="remote-turn-off" disabled={off.isPending} onClick={() => off.mutate()}>Turn off</Button>}
      </div>
      {!view.on && view.off_reason === 'hub_full' && <p data-testid="remote-hub-note" className="text-[13px] text-text-muted">It was turned off because the hub now reads this workspace.</p>}
      {!view.on && !turning && <OffActions view={view} onTurnOn={() => setTurning(true)} />}
      {!view.on && turning && <TurnOn ws={ws} view={view} onClose={() => { setTurning(false); refresh() }} />}

      {view.on && (
        <div className="space-y-3">
          <h3 className="text-[13px] font-medium">Devices</h3>
          {view.devices.length === 0
            ? <p data-testid="remote-no-devices" className="text-[13px] text-text-muted">No devices yet.</p>
            : <ul className="space-y-2">
              {view.devices.map(d => (
                <li key={d.device_id} data-testid="remote-device" className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-[13px]">
                  <span className="min-w-0"><strong><Raw>{d.name}</Raw></strong> <span className="text-text-muted">{LEVEL_NAME[d.level_cap]}, {untilText(d.expires_ms)}</span></span>
                  <span className="flex items-center gap-1">
                    <InfoDrawer label="Technical details" testId="remote-device-info"><p>Fingerprint: <Raw>{d.words}</Raw></p><p>Device id: <Raw>{d.device_id}</Raw></p></InfoDrawer>
                    <Button size="sm" variant="outline" data-testid="remote-device-remove" disabled={remove.isPending} onClick={() => remove.mutate(d.device_id)}>Remove</Button>
                  </span>
                </li>
              ))}
            </ul>}
          {waiting.map(p => <PendingRow key={p.pending} ws={ws} view={view} row={p} onDone={refresh} />)}
          {code && waiting.length === 0 && <CodeBox code={code} />}
          {!code && waiting.length === 0 && <div><Button size="sm" data-testid="remote-add-device" disabled={issue.isPending} onClick={() => issue.mutate()}>Add a device</Button></div>}
        </div>
      )}

      {view.on && (
        <div className="space-y-2">
          <h3 className="text-[13px] font-medium">Open sessions</h3>
          {view.sessions.length === 0
            ? <p data-testid="remote-no-sessions" className="text-[13px] text-text-muted">No one is connected.</p>
            : <ul className="space-y-1">{view.sessions.map(s => <li key={s.device_id} data-testid="remote-session" className="text-[13px]"><Raw>{s.device}</Raw></li>)}</ul>}
        </div>
      )}
    </section>
  )
}
