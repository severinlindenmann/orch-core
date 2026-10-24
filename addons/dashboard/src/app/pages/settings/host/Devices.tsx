import { Link } from '@tanstack/react-router'
import { useHub } from '../hub/shared'
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
import { checkSas, convenienceOffCommand, normaliseSas, SAS_ATTEMPTS, SIGN_COMMAND, pairingApi, pairQueries, pairServed, PAIR_UNAVAILABLE, primaryCommand, type PairBlocked, pairedDevices, type PairDevice, type PairOffer, type PairOfferStatus, type PairPreview } from '@/api/pairing'
import { ApiError, type Workspace } from '@/api/types'
import { hasFeature } from '@/api/features'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline, Prose } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { fmtDateTime } from '@/lib/time'
import { Mono, Pill } from '../../ticket/shared'
import { useSettingsHandoff } from './useSettingsHandoff'
import { ReaderLevels } from './Readers'
import { ReplaceKey } from './ReplaceKey'
import { refusalOf, refusalSentence } from '@/api/refusals'
import { RefusalDevices } from '@/components/sign/RefusalDevices'

const box = 'space-y-3 rounded-md border border-border px-3 py-3 text-[13px]'

/** A command or code the person copies by selecting it: plain text, never a link. */
function CopyText({ children, label, testId }: { children: string; label: string; testId?: string }) {
  return (
    <code aria-label={label} data-testid={testId} className="block select-all break-all rounded bg-surface-3 px-2 py-1 font-mono text-[12px] text-text">
      {children}
    </code>
  )
}

function ErrorLine({ error, ws }: { error: unknown; ws: string }) {
  const e = error instanceof ApiError ? error : null
  // H12d-2: a click-to-sign readers refusal or a declaration that did not verify, in plain words first.
  const refusal = refusalOf(error)
  return (
    <div role="alert" className="space-y-1 text-[13px]">
      {refusal && (
        <p data-testid="pair-refusal" data-reason={refusal.reason ?? ''} className="text-danger">
          {refusalSentence(refusal)}
        </p>
      )}
      {refusal && <RefusalDevices ws={ws} ids={refusal.devices} />}
      <p className={refusal ? 'text-text-muted' : 'text-danger'}>
        Not done. {e ? <Mono>{e.code}</Mono> : null} {e && e.message !== e.code ? <Inline>{e.message}</Inline> : !e ? 'Try again in a moment.' : null}
      </p>
    </div>
  )
}

/** Settings → Devices: the reader levels (H12d-2), then pairing. Without the pairing gate: one calm line, no pairing call. */
export function HostDevices({ workspace }: { workspace: Workspace }) {
  const hub = useHub(workspace.id)
  return (
    <div className="space-y-6">
      {hub.data?.mode === 'standalone' && <p data-testid="devices-needs-hub" className="text-sm text-text-muted">Needs a hub · Phone access needs a hub. <Link data-testid="devices-connect-hub" to="/settings/$tab" params={{ tab: 'hub' }} className="text-chart-4 underline">Connect a hub</Link></p>}
      {!pairServed(workspace.id) ? (
        <div className="space-y-3">
          <h2 className="text-base font-semibold">Devices</h2>
          <p data-testid="pair-unavailable" className="rounded-md border border-border px-3 py-2 text-[13px] text-text-muted">
            {PAIR_UNAVAILABLE}
          </p>
        </div>
      ) : (
        <Pairing workspace={workspace} />
      )}
      <ReplaceKey workspace={workspace} />
      <ReaderLevels ws={workspace.id} />
    </div>
  )
}

function Pairing({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const qc = useQueryClient()
  const preview = useQuery(pairQueries.preview(ws))
  const list = useQuery(pairQueries.list(ws))
  const members = useQuery(pairQueries.members(ws))
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
        {error && !(error instanceof ApiError && preview.data?.blocked?.code === error.code) ? <ErrorLine error={error} ws={ws} /> : null}
      </section>
      <DeviceList workspace={workspace} devices={members.data ? pairedDevices(members.data.members) : null} failed={members.isError} />
    </div>
  )
}

function Blocked({ blocked, workspace }: { blocked: PairBlocked; workspace: Workspace }) {
  const convenience = blocked.code === 'sign.convenience_readers'
  // H12d-2: the preview's `blocked.detail` has the same closed keys as `Error.detail`.
  const refusal = convenience ? refusalOf(new ApiError(409, { code: blocked.code, message: blocked.message, retryable: false, detail: blocked.detail })) : null
  return (
    <div role="alert" data-testid="pair-blocked" data-code={blocked.code} className="space-y-2 rounded-md border border-warning/40 px-3 py-3 text-[13px]">
      <p className="font-medium">
        {convenience ? 'This phone’s orch app is too old to pair with a workspace that uses click-to-sign.' : 'A phone cannot be paired on this workspace now.'}{' '}
        <Mono>{blocked.code}</Mono>
      </p>
      {refusal?.reason && <p data-testid="pair-blocked-reason">{refusalSentence(refusal)}</p>}
      <p className="text-text-muted" aria-label="What the host says">
        The host says: <Inline>{blocked.message}</Inline>
      </p>
      {convenience && (
        <div data-testid="pair-blocked-convenience" className="space-y-2">
          <p data-testid="pair-convenience-off">
            Update the orch app on the phone and scan again, or turn click-to-sign off first:{' '}
            {convenienceOffCommand(workspace.prefix) !== null ? (
              <code className="select-all break-all rounded bg-surface-3 px-1 font-mono text-[12px] text-text">{convenienceOffCommand(workspace.prefix)}</code>
            ) : (
              <span data-testid="pair-command-unsafe">run orch host convenience off for this workspace (its prefix has characters that can&apos;t be put in a command safely)</span>
            )}
          </p>
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
        Pairing makes a one-time link for the orch app on your phone. You compare a code on both screens, confirm on your computer&apos;s primary, and sign
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

/** The host's label as untrusted text: escaped and isolated, an LF shown as a line break (never markup). Empty: "phone". */
export function DeviceLabel({ label, named = true }: { label: string | null | undefined; named?: boolean }) {
  return label ? (
    <span data-testid="pair-label">
      “<Prose inline>{label}</Prose>”{named && <span className="text-text-muted"> (named by the device)</span>}
    </span>
  ) : (
    <span data-testid="pair-label">phone</span>
  )
}

function CompareCode({ ws, status, onCancel, refresh }: { ws: string; status: PairOfferStatus; onCancel: () => void; refresh: () => void }) {
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  // Each distinct full-length mismatch is one attempt; the third refuses the offer.
  const [misses, setMisses] = useState<readonly string[]>([])
  const check = checkSas(typed, status.sas)
  const left = SAS_ATTEMPTS - misses.length
  const answer = async (approve: boolean, mismatches?: number) => {
    setBusy(true)
    setError(null)
    try {
      await pairingApi.answer(ws, status.offer_id, approve, mismatches ? { mismatches } : undefined)
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
        A phone connected: <DeviceLabel label={status.label} />.
      </p>
      <Scopes scopes={status.scopes} />
      <div className="space-y-1">
        <label htmlFor="pair-sas" className="block">
          Type the six characters your phone shows
        </label>
        <input
          id="pair-sas"
          value={typed}
          onChange={(e) => {
            const value = e.target.value
            setTyped(value)
            const n = normaliseSas(value)
            if (checkSas(value, status.sas) === 'differ' && n.length === normaliseSas(status.sas ?? '').length && !misses.includes(n) && misses.length < SAS_ATTEMPTS) {
              const next = [...misses, n]
              setMisses(next)
              if (next.length >= SAS_ATTEMPTS) void answer(false, next.length)
            }
          }}
          disabled={busy || left <= 0}
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
        {misses.length > 0 && (
          <p data-testid="pair-sas-attempts" className="text-text-muted">
            {left > 0 ? `${left} ${left === 1 ? 'attempt' : 'attempts'} left. After ${SAS_ATTEMPTS} wrong codes the phone is refused.` : 'No attempts left: the phone is refused.'}
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
      {error ? <ErrorLine error={error} ws={ws} /> : null}
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
        <p className="font-medium">Codes matched. The host is asking your computer&apos;s primary for a certificate.</p>
        <Button size="sm" variant="ghost" onClick={p.onCancel}>
          Cancel pairing
        </Button>
      </div>
    )
  if (state === 'cert_pending')
    return (
      <div className={box} data-testid="pair-cert">
        <p className="font-medium">Two steps in a terminal on this computer finish the pairing.</p>
        <p className="text-text-muted">
          They can&apos;t be done in the browser: a workspace with a paired phone refuses click-to-sign.
        </p>
        <ol className="list-decimal space-y-3 pl-5">
          <li className="space-y-1" data-testid="pair-step-primary">
            <p>Confirm on your computer&apos;s primary. Run:</p>
            {primaryCommand(status?.offer_id) !== null ? (
              <CopyText label="Command for the primary" testId="pair-primary-command">
                {primaryCommand(status?.offer_id)!}
              </CopyText>
            ) : (
              <p role="alert" data-testid="pair-command-unsafe" className="text-warning">The offer id can&apos;t be put in a command safely. Cancel this pairing and start again.</p>
            )}
            <p>It shows a code. Confirm only if it is this one:</p>
            <p data-testid="pair-cert-code" className="select-all font-mono text-2xl tracking-[0.3em] text-text">
              {status?.cert_code ?? '……'}
            </p>
            <p className="text-text-muted">
              For <DeviceLabel label={status?.label} />.
            </p>
            {status?.cert_ready && <p className="text-success">Done: the primary signed the certificate.</p>}
          </li>
          <li className="space-y-1" data-testid="pair-step-sign">
            <p>Sign the new device into this workspace. Run, with the code the dashboard gives you:</p>
            <CopyText label="Command to sign the new device" testId="pair-sign-command">
              {SIGN_COMMAND}
            </CopyText>
            {status?.cert_ready ? (
              <Button size="sm" onClick={() => handoff.ask({ title: 'Add the phone', action: { action: 'pair_device', offer: status.offer_id }, autoStart: true })}>
                Get the signing code
              </Button>
            ) : (
              <p className="text-text-muted">The code is ready once the primary has signed (step 1).</p>
            )}
          </li>
        </ol>
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
          <DeviceLabel label={status?.label} /> is paired.
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
              If the phone was already added (<DeviceLabel label={status?.label} />), it stays in the workspace but cannot reach the relay: remove the device below.
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

function DeviceList({ workspace, devices, failed }: { workspace: Workspace; devices: PairDevice[] | null; failed: boolean }) {
  const qc = useQueryClient()
  const handoff = useSettingsHandoff(workspace.id, () => qc.invalidateQueries({ queryKey: ['pair', workspace.id] }))
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
                <TableHead>Member</TableHead>
                <TableHead>Paired</TableHead>
                <TableHead>Relay</TableHead>
                <TableHead>Certificate until</TableHead>
                <TableHead>Reader</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {devices.map((d) => (
                <TableRow key={d.id} data-testid="device-row" className="align-top">
                  <TableCell className="max-w-64 whitespace-normal">
                    <DeviceLabel label={d.label} named={false} />
                    <div>
                      <Mono className="break-all text-text-muted"><Inline>{String(d.id)}</Inline></Mono>
                    </div>
                    {hasFeature('do:device.remove', workspace.id) ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        data-testid="device-remove"
                        className="mt-1 text-danger"
                        onClick={() =>
                          handoff.ask({
                            title: 'Remove this device',
                            action: { action: 'device_remove', device: String(d.id) },
                            autoStart: true,
                            confirmLabel: 'Remove device',
                            summary: (
                              <div data-testid="device-remove-summary" className="space-y-1">
                                <p>
                                  <DeviceLabel label={d.label} named={false} /> of <Inline>{d.name}</Inline>
                                </p>
                                <p>This device will no longer be able to approve changes for this workspace. Pair it again to restore that.</p>
                              </div>
                            ),
                          })
                        }
                      >
                        Remove
                      </Button>
                    ) : null}
                  </TableCell>
                  <TableCell className="whitespace-normal">
                    <Inline>{d.name}</Inline>
                  </TableCell>
                  <TableCell>{d.paired_at ? fmtDateTime(d.paired_at) : '—'}</TableCell>
                  <TableCell>{d.relay ? 'yes' : 'no'}</TableCell>
                  <TableCell>{d.cert_expires_at ? fmtDateTime(d.cert_expires_at) : '—'}</TableCell>
                  <TableCell>{d.reader ?? 'none'}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {handoff.dialog}
    </section>
  )
}
