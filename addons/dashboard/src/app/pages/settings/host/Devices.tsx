// Settings → Devices on a real host: pair a phone and list the paired devices (PROPOSED ops, cap `pair/1`, behind the
// feature gate; src/api/pairing.ts). The dashboard never signs: adding the phone (`relay.pair_device`) and removing a
// device (`device.remove`) are hand-offs; the primary's certificate step stays in the terminal.
//
// The pairing link is a secret. It lives only in `LinkPanel`'s parent state (`offer`), is never put in a query, a
// mutation, storage, the URL or a log, and is dropped at the first state past `open`, at expiry, on cancel and on
// unmount. It is never fetched again: a lost link means cancel and a new offer.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { encode } from 'uqr'
import { checkSas, convenienceOffCommand, pairingApi, pairQueries, pairServed, PAIR_UNAVAILABLE, PRIMARY_COMMAND, type PairBlocked, type PairDevice, type PairOffer, type PairOfferStatus, type PairPreview } from '@/api/pairing'
import { ApiError, type Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline, plain } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { fmtDateTime } from '@/lib/time'
import { Mono, Pill } from '../../ticket/shared'
import { useSettingsHandoff } from './useSettingsHandoff'

const box = 'space-y-3 rounded-md border border-border px-3 py-3 text-[13px]'

/** A command or code the person copies by selecting it: plain text, never a link. */
function CopyText({ children, label, testId }: { children: string; label: string; testId?: string }) {
  return (
    <code aria-label={label} data-testid={testId} className="block select-all break-all rounded bg-surface-3 px-2 py-1 font-mono text-[12px] text-text">
      {children}
    </code>
  )
}

function ErrorLine({ error }: { error: unknown }) {
  const e = error instanceof ApiError ? error : null
  return (
    <p role="alert" className="text-[13px] text-danger">
      Not done. {e ? <Mono>{e.code}</Mono> : null} {e && e.message !== e.code ? <Inline>{e.message}</Inline> : !e ? 'Try again in a moment.' : null}
    </p>
  )
}

/** Settings → Devices. Without the gate: one calm line, no call to the host. */
export function HostDevices({ workspace }: { workspace: Workspace }) {
  if (!pairServed(workspace.id))
    return (
      <div className="space-y-3">
        <h2 className="text-base font-semibold">Devices</h2>
        <p data-testid="pair-unavailable" className="rounded-md border border-border px-3 py-2 text-[13px] text-text-muted">
          {PAIR_UNAVAILABLE}
        </p>
      </div>
    )
  return <Pairing workspace={workspace} />
}

function Pairing({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const qc = useQueryClient()
  const preview = useQuery(pairQueries.preview(ws))
  const list = useQuery(pairQueries.list(ws))
  const devices = useQuery(pairQueries.devices(ws))
  // The link: component state only.
  const [offer, setOffer] = useState<PairOffer | null>(null)
  const offerRef = useRef<PairOffer | null>(null)
  offerRef.current = offer
  /** The offer this page follows (its own, or the host's live one); null: none. */
  const [follow, setFollow] = useState<string | null>(null)
  const [dismissed, setDismissed] = useState<ReadonlySet<string>>(new Set())
  const [expiredHere, setExpiredHere] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<unknown>(null)

  // Unmount: nothing of the link stays behind.
  useEffect(() => () => void (offerRef.current = null), [])

  const refresh = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ['pair', ws] })
  }, [qc, ws])

  // Live: `pair` sub items carry no secrets. The first state past `open` drops the link at once.
  useEffect(
    () =>
      pairingApi.subscribe(ws, (item) => {
        if (offerRef.current?.offer_id === item.offer_id && item.state !== 'open') setOffer(null)
        refresh()
      }),
    [ws, refresh],
  )

  const live = preview.data?.live_offer ?? null
  const currentId = follow ?? (live && !dismissed.has(live) ? live : null)
  // Keep following the host's live offer once shown, also after it ends (its outcome stays until "Start again").
  useEffect(() => {
    if (!follow && live && !dismissed.has(live)) setFollow(live)
  }, [follow, live, dismissed])
  const status = list.data?.offers.find((o) => o.offer_id === currentId) ?? null

  // Belt and braces: whatever the list says past `open` also drops the link.
  useEffect(() => {
    if (offer && status && status.offer_id === offer.offer_id && status.state !== 'open') setOffer(null)
  }, [offer, status])

  // Expiry from the host's `expires_ms`: drop the link and say so. A timer alone can run late (a hidden tab is throttled),
  // so every second and whenever the tab shows again the page also checks the clock.
  const expireIfDue = useCallback(() => {
    const o = offerRef.current
    if (!o || Date.now() < o.expires_ms) return
    offerRef.current = null
    setOffer((cur) => (cur?.offer_id === o.offer_id ? null : cur))
    setExpiredHere(o.offer_id)
    refresh()
  }, [refresh])
  useEffect(() => {
    if (!offer) return
    const t = setTimeout(expireIfDue, Math.max(0, offer.expires_ms - Date.now()))
    const tick = setInterval(expireIfDue, 1000)
    const onShow = () => document.visibilityState !== 'hidden' && expireIfDue()
    document.addEventListener('visibilitychange', onShow)
    window.addEventListener('focus', onShow)
    return () => {
      clearTimeout(t)
      clearInterval(tick)
      document.removeEventListener('visibilitychange', onShow)
      window.removeEventListener('focus', onShow)
    }
  }, [offer, expireIfDue])

  const create = async () => {
    setCreating(true)
    setError(null)
    try {
      const made = await pairingApi.offer(ws)
      setExpiredHere(null)
      setFollow(made.offer_id)
      setOffer(made)
    } catch (e) {
      setError(e)
    } finally {
      setCreating(false)
      refresh()
    }
  }

  const cancel = async (id: string) => {
    setError(null)
    setOffer(null)
    try {
      await pairingApi.cancel(ws, id)
    } catch (e) {
      setError(e)
    } finally {
      refresh()
    }
  }

  const done = (id: string) => {
    setOffer(null)
    setFollow(null)
    setExpiredHere(null)
    setDismissed((d) => new Set(d).add(id))
    refresh()
  }

  if (preview.isError || list.isError) return <LoadFailed what="pairing" onRetry={refresh} />

  const state = currentId && expiredHere === currentId && (!status || status.state === 'open') ? 'expired' : (status?.state ?? (offer ? 'open' : null))

  return (
    <div className="space-y-6">
      <section className="space-y-3" aria-labelledby="pair-heading">
        <h2 id="pair-heading" className="text-base font-semibold">
          Pair a phone
        </h2>
        {!preview.data ? (
          <p className="text-[13px] text-text-muted">Loading…</p>
        ) : currentId && state ? (
          <OfferView
            workspace={workspace}
            state={state}
            status={status}
            offer={offer && offer.offer_id === currentId ? offer : null}
            onCancel={() => void cancel(currentId)}
            onDone={() => done(currentId)}
            refresh={refresh}
          />
        ) : (
          <BeforeOffer workspace={workspace} preview={preview.data} creating={creating} onCreate={() => void create()} />
        )}
        {error && !(error instanceof ApiError && preview.data?.blocked?.code === error.code) ? <ErrorLine error={error} /> : null}
      </section>
      <DeviceList workspace={workspace} devices={devices.data?.devices ?? null} failed={devices.isError} refresh={refresh} />
    </div>
  )
}

function Blocked({ blocked, workspace }: { blocked: PairBlocked; workspace: Workspace }) {
  const convenience = blocked.code === 'sign.convenience_readers'
  return (
    <div role="alert" data-testid="pair-blocked" data-code={blocked.code} className="space-y-2 rounded-md border border-warning/40 px-3 py-3 text-[13px]">
      <p className="font-medium">
        {convenience ? 'A phone cannot be paired while click-to-sign is on, or was used, on this workspace.' : 'A phone cannot be paired on this workspace now.'}{' '}
        <Mono>{blocked.code}</Mono>
      </p>
      <p className="text-text-muted" aria-label="What the host says">
        The host says: <Inline>{blocked.message}</Inline>
      </p>
      {convenience && (
        <div data-testid="pair-blocked-convenience" className="space-y-2">
          <p>
            A phone reads this workspace&apos;s log, and a phone app cannot check click-to-sign signatures yet, so the host refuses to add one where
            click-to-sign could have signed.
          </p>
          {blocked.history !== true && (
            <div className="space-y-1">
              <p>{blocked.history === false ? 'Click-to-sign is on but was never used here.' : 'If click-to-sign was never used here,'} Turn it off first in a terminal on this Mac, then come back:</p>
              <CopyText label="Command to turn click-to-sign off">{convenienceOffCommand(workspace.prefix)}</CopyText>
            </div>
          )}
          {blocked.history !== false && (
            <p>
              {blocked.history === true ? 'Click-to-sign was used on this workspace.' : 'If it was used here,'} A workspace that used click-to-sign can&apos;t get a phone
              yet: turning it off does not change that.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function BeforeOffer({ workspace, preview, creating, onCreate }: { workspace: Workspace; preview: PairPreview; creating: boolean; onCreate: () => void }) {
  if (preview.blocked) return <Blocked blocked={preview.blocked} workspace={workspace} />
  return (
    <div className={box} data-testid="pair-before">
      <p>
        Pairing makes a one-time link for the orch app on your phone. You compare a code on both screens, confirm on your Mac&apos;s primary, and sign
        the new device in a terminal.
      </p>
      <p className="text-text-muted">
        The phone may: <span data-testid="pair-scopes">{preview.scopes_default.join(', ')}</span>.
      </p>
      {preview.warnings.length > 0 && (
        <ul aria-label="Before you pair" data-testid="pair-warnings" className="list-disc space-y-1 pl-5 text-warning">
          {preview.warnings.map((w, i) => (
            <li key={i}>
              <Inline>{w}</Inline>
            </li>
          ))}
        </ul>
      )}
      <Button size="sm" onClick={onCreate} disabled={creating}>
        Create pairing link
      </Button>
    </div>
  )
}

function useCountdown(until: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (until === null) return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [until])
  return until === null ? 0 : Math.max(0, until - now)
}

const mmss = (ms: number) => {
  const s = Math.ceil(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/** The SVG path of a QR for `value` (pure; the tests compare it with the native link's). */
export function qrPath(value: string): { d: string; size: number } {
  const qr = encode(value, { ecc: 'M', border: 2 })
  let d = ''
  qr.data.forEach((row, y) => row.forEach((on, x) => on && (d += `M${x} ${y}h1v1h-1z`)))
  return { d, size: qr.size }
}

/** The QR, drawn here from memory (no network, no service). It carries the native `orch://` link only. */
export function PairQr({ value }: { value: string }) {
  const path = useMemo(() => qrPath(value), [value])
  return (
    <svg role="img" aria-label="Pairing QR code" data-testid="pair-qr" viewBox={`0 0 ${path.size} ${path.size}`} className="size-56 shrink-0 rounded bg-white" shapeRendering="crispEdges">
      <path d={path.d} className="fill-black" />
    </svg>
  )
}

function LinkPanel({ offer, onCancel }: { offer: PairOffer; onCancel: () => void }) {
  // Everything that carries the secret (QR, typed fallback, Copy link) waits for one explicit reveal.
  const [shown, setShown] = useState(false)
  const [copied, setCopied] = useState(false)
  const copiedRef = useRef(false)
  const left = useCountdown(offer.expires_ms)
  // When the link is dropped (pending, expiry, cancel, unmount) and this panel copied it, overwrite the clipboard.
  useEffect(
    () => () => {
      if (copiedRef.current) void navigator.clipboard?.writeText('').catch(() => {})
    },
    [],
  )
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(offer.native_link)
      copiedRef.current = true
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className={box} data-testid="pair-waiting">
      <p className="font-medium">Waiting for the phone.</p>
      <p className="text-text-muted">
        Open the orch app on your phone and scan the code. The link works once and expires in <span data-testid="pair-countdown">{mmss(left)}</span>.
        Anyone who sees it can try to pair, so show it only to your phone (not while sharing your screen).
      </p>
      {!shown ? (
        <Button size="sm" variant="outline" onClick={() => setShown(true)}>
          Show QR and link
        </Button>
      ) : (
        <>
          <div className="flex flex-wrap items-start gap-3">
            <PairQr value={offer.native_link} />
            <div className="space-y-1">
              <Button size="sm" variant="outline" onClick={() => void copy()}>
                {copied ? 'Copied' : 'Copy link'}
              </Button>
              {copied && <p className="text-[12px] text-text-muted">The page clears your clipboard when this link is no longer shown.</p>}
            </div>
          </div>
          <div className="space-y-1">
            <p className="text-text-muted">If scanning fails, type this on the phone:</p>
            <CopyText label="Pairing link to type on the phone" testId="pair-fallback">
              {offer.link}
            </CopyText>
          </div>
        </>
      )}
      <Button size="sm" variant="ghost" onClick={onCancel}>
        Cancel pairing
      </Button>
    </div>
  )
}

function Scopes({ scopes }: { scopes: string[] }) {
  return (
    <ul aria-label="What the phone asks for" className="flex flex-wrap gap-1.5">
      {scopes.map((s) => (
        <li key={s}>
          <Pill>{s}</Pill>
        </li>
      ))}
    </ul>
  )
}

function Label({ label }: { label: string | null | undefined }) {
  return label ? (
    <span data-testid="pair-label">
      “<Inline>{label}</Inline>” <span className="text-text-muted">(named by the device)</span>
    </span>
  ) : (
    <span className="text-text-muted">a device without a name</span>
  )
}

function CompareCode({ ws, status, onCancel, refresh }: { ws: string; status: PairOfferStatus; onCancel: () => void; refresh: () => void }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const check = checkSas(typed, status.sas)
  const answer = async (approve: boolean) => {
    setBusy(true)
    setError(null)
    try {
      await pairingApi.answer(ws, status.offer_id, approve)
    } catch (e) {
      setError(e)
    } finally {
      setBusy(false)
      refresh()
    }
  }
  return (
    <div className={box} data-testid="pair-compare">
      <p className="font-medium">
        A phone connected: <Label label={status.label} />.
      </p>
      <Scopes scopes={status.scopes} />
      <div className="space-y-1">
        <label htmlFor="pair-sas" className="block">
          Type the six characters your phone shows
        </label>
        <input
          id="pair-sas"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          maxLength={16}
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
          className="w-48 rounded-md border border-border bg-surface-2 px-3 py-2 font-mono text-2xl tracking-[0.3em] uppercase text-text"
        />
        {check === 'match' && <p className="text-success">The codes match.</p>}
        {check === 'differ' && (
          <p role="alert" data-testid="pair-sas-differ" className="font-medium text-danger">
            The codes differ: reject this phone.
          </p>
        )}
        {check !== 'match' && check !== 'differ' && <p className="text-text-muted">Approve stays off until the codes match. If you are not sure, reject.</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        {/* Reject is the default and has the focus; Approve needs the matching code and its own click. */}
        <Button size="sm" variant="destructive" autoFocus disabled={busy} onClick={() => void answer(false)}>
          Reject
        </Button>
        <Button size="sm" variant="outline" disabled={busy || check !== 'match'} onClick={() => void answer(true)}>
          Approve
        </Button>
      </div>
      {error ? <ErrorLine error={error} /> : null}
      <Button size="sm" variant="ghost" onClick={onCancel}>
        Cancel pairing
      </Button>
    </div>
  )
}

const END: Record<string, { title: string; tone: 'ok' | 'warn' }> = {
  approved: { title: 'Paired.', tone: 'ok' },
  rejected: { title: 'Refused.', tone: 'warn' },
  expired: { title: 'The pairing link expired.', tone: 'warn' },
  cancelled: { title: 'Pairing cancelled.', tone: 'warn' },
  failed: { title: 'Pairing failed.', tone: 'warn' },
}

function OfferView(p: {
  workspace: Workspace
  state: string
  status: PairOfferStatus | null
  offer: PairOffer | null
  onCancel: () => void
  onDone: () => void
  refresh: () => void
}) {
  const { workspace, state, status, offer } = p
  const handoff = useSettingsHandoff(workspace.id, p.refresh)
  if (state === 'open')
    return offer ? (
      <LinkPanel offer={offer} onCancel={p.onCancel} />
    ) : (
      <div className={box} data-testid="pair-lost-link">
        <p className="font-medium">A pairing link is open, but this page can&apos;t show it again.</p>
        <p className="text-text-muted">The link is shown once. Cancel it and create a new one.</p>
        <Button size="sm" variant="outline" onClick={p.onCancel}>
          Cancel pairing
        </Button>
      </div>
    )
  if (state === 'pending' && status) return <CompareCode ws={workspace.id} status={status} onCancel={p.onCancel} refresh={p.refresh} />
  if (state === 'confirmed')
    return (
      <div className={box} data-testid="pair-confirmed">
        <p className="font-medium">Codes matched. The host is asking your Mac&apos;s primary for a certificate.</p>
        <Button size="sm" variant="ghost" onClick={p.onCancel}>
          Cancel pairing
        </Button>
      </div>
    )
  if (state === 'cert_pending')
    return (
      <div className={box} data-testid="pair-cert">
        {status?.cert_ready ? (
          <>
            <p className="font-medium">The primary signed the certificate. Sign to add the phone to this workspace.</p>
            <Button
              size="sm"
              onClick={() => handoff.ask({ title: 'Add the phone', action: { action: 'pair_device', offer: status.offer_id }, autoStart: true })}
            >
              Add the phone
            </Button>
          </>
        ) : (
          <>
            <p className="font-medium">Confirm on your Mac&apos;s primary.</p>
            <p>In a terminal on this Mac, run:</p>
            <CopyText label="Command for the primary" testId="pair-primary-command">
              {PRIMARY_COMMAND}
            </CopyText>
            <p>It shows a code. Confirm only if it is this one:</p>
            <p data-testid="pair-cert-code" className="select-all font-mono text-2xl tracking-[0.3em] text-text">
              {status?.cert_code ?? '……'}
            </p>
            <p className="text-text-muted">
              For <Label label={status?.label} />.
            </p>
          </>
        )}
        <Button size="sm" variant="ghost" onClick={p.onCancel}>
          Cancel pairing
        </Button>
        {handoff.dialog}
      </div>
    )
  if (state === 'allocated' || state === 'published')
    return (
      <div className={box} data-testid="pair-adding">
        <p className="font-medium">Adding the phone…</p>
        <p className="text-text-muted">The device is in the workspace&apos;s log; the host is telling the relay. This can no longer be cancelled.</p>
      </div>
    )
  const end = END[state]
  return (
    <div className={box} data-testid={`pair-end-${state}`} role="status">
      <p className={end?.tone === 'ok' ? 'font-medium text-success' : 'font-medium text-warning'}>{end?.title ?? `State ${state}.`}</p>
      {state === 'approved' && (
        <p>
          <Label label={status?.label} /> is paired.
        </p>
      )}
      {status?.reason && (state === 'rejected' || state === 'failed') && (
        <p>
          Reason from the host: <Inline>{status.reason}</Inline>
        </p>
      )}
      {state === 'failed' && (
        <p data-testid="pair-needs-remove">
          {status?.needs_remove ? (
            <Inline>{status.needs_remove}</Inline>
          ) : (
            <>
              If the phone was already added (<Label label={status?.label} />), it stays in the workspace but cannot reach the relay: remove the device below.
            </>
          )}
        </p>
      )}
      <Button size="sm" variant="outline" onClick={p.onDone}>
        {state === 'approved' ? 'Done' : 'Start again'}
      </Button>
    </div>
  )
}

function DeviceList({ workspace, devices, failed, refresh }: { workspace: Workspace; devices: PairDevice[] | null; failed: boolean; refresh: () => void }) {
  const handoff = useSettingsHandoff(workspace.id, refresh)
  return (
    <section className="space-y-3" aria-labelledby="devices-heading">
      <h2 id="devices-heading" className="text-base font-semibold">
        Paired devices
      </h2>
      {failed ? (
        <p role="alert" className="text-[13px] text-text-muted">
          The device list could not be loaded.
        </p>
      ) : !devices ? (
        <p className="text-[13px] text-text-muted">Loading…</p>
      ) : devices.length === 0 ? (
        <p data-testid="devices-empty" className="text-[13px] text-text-muted">
          No phone is paired with this workspace.
        </p>
      ) : (
        <div className="overflow-x-auto" data-scroll-x>
          <Table aria-label="Paired devices">
            <TableHeader>
              <TableRow>
                <TableHead>Device (named by the device)</TableHead>
                <TableHead>Paired</TableHead>
                <TableHead>Relay</TableHead>
                <TableHead>Certificate until</TableHead>
                <TableHead>Reader</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {devices.map((d) => (
                <TableRow key={d.device} data-testid="device-row">
                  <TableCell className="max-w-64 whitespace-normal">
                    {d.label ? <Inline>{d.label}</Inline> : <span className="text-text-muted">no name</span>}
                    <div>
                      <Mono className="break-all text-text-muted">{d.device}</Mono>
                    </div>
                  </TableCell>
                  <TableCell>{d.paired_ms ? fmtDateTime(new Date(d.paired_ms).toISOString()) : '—'}</TableCell>
                  <TableCell>{d.relay ? 'yes' : 'no'}</TableCell>
                  <TableCell>{d.cert_expires ? fmtDateTime(d.cert_expires) : '—'}</TableCell>
                  <TableCell>{d.reader ?? '—'}</TableCell>
                  <TableCell>
                    <Button
                      size="xs"
                      variant="outline"
                      onClick={() =>
                        handoff.ask({
                          title: d.label ? `Remove ${plain(d.label)}` : 'Remove the device',
                          action: { action: 'device_remove', device: d.device },
                          autoStart: true,
                          summary: (
                            <p data-testid="device-remove-rotates" className="text-[13px] text-warning">
                              This also rotates the relay keys.
                            </p>
                          ),
                        })
                      }
                    >
                      Remove
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {devices && devices.length > 0 && <p className="text-[12px] text-text-muted">Removing a device also rotates the relay keys.</p>}
      {handoff.dialog}
    </section>
  )
}
