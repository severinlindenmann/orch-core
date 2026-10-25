// Settings, Remote access (variant A: one page of cards; #1198 R1-8): the address, the passkeys, "Add a device", "Log out
// everywhere", and the problem state. Removing a passkey and adding a device each need a touch (step-up). The last passkey
// can never be removed, and the page says why in words. Plain words: no ids, no commands.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Fingerprint } from 'lucide-react'
import { remoteInfo } from '@/api/features'
import { createEnrollLink, listPasskeys, logoutEverywhere, PASSKEY_WORDS, PasskeyError, removePasskey, type EnrollLink, type PasskeyRow } from '@/api/passkey'
import { clearSession } from '@/api/bootstrap'
import { copyText } from '@/app/copyLink'
import { Button } from '@/components/ui/button'
import { StepUpNote, needsTouch, TOUCH_LABEL, useStepUpPhase } from '@/components/sign/StepUp'
import { resetStepUp } from '@/api/passkey'
import { Section } from '../../ticket/shared'
import { qrPath } from './Devices'

const when = (stamp: string | null) => {
  if (!stamp) return 'Never'
  const t = Date.parse(stamp)
  if (Number.isNaN(t)) return 'Unknown'
  const mins = Math.round((Date.now() - t) / 60_000)
  if (mins < 2) return 'Just now'
  if (mins < 60) return `${mins} minutes ago`
  return new Date(t).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

const Pill = ({ tone, children }: { tone: 'ok' | 'warn'; children: string }) => (
  <span className={`ml-2 rounded-full px-2.5 py-px text-xs font-normal ${tone === 'ok' ? 'bg-brand-soft text-brand' : 'bg-warning-soft text-warning'}`}>{children}</span>
)

export function RemoteAccess() {
  const { address, problem } = remoteInfo()
  const qc = useQueryClient()
  const passkeys = useQuery({ queryKey: ['passkeys'], queryFn: listPasskeys, enabled: address !== null })
  const rows = passkeys.data ?? []
  const addRef = useRef<HTMLDivElement>(null)
  return (
    <div className="space-y-3" data-testid="remote-access">
      <h2 className="text-lg font-semibold">Remote access</h2>
      <AddressCard address={address} problem={problem} onCheck={() => void qc.invalidateQueries({ queryKey: ['session'] })} />
      {address !== null && (
        <>
          <PasskeysCard rows={rows} loading={passkeys.isPending} failed={passkeys.isError} onAdd={() => addRef.current?.scrollIntoView({ block: 'center' })} onChanged={() => void qc.invalidateQueries({ queryKey: ['passkeys'] })} />
          <div ref={addRef}><AddDeviceCard count={rows.length} onUsed={() => void qc.invalidateQueries({ queryKey: ['passkeys'] })} /></div>
          <LogoutCard />
        </>
      )}
      {address === null && problem === null && <p className="text-[13px] text-text-muted">Remote access is off.</p>}
    </div>
  )
}

function AddressCard({ address, problem, onCheck }: { address: string | null; problem: string | null; onCheck: () => void }) {
  const [copied, setCopied] = useState<string | null>(null)
  const copy = async (what: string, text: string) => { if (await copyText(text)) { setCopied(what); setTimeout(() => setCopied(null), 2500) } }
  const details = `orch remote access is not working.\nWhat orch measured: ${problem}\nChecked: ${new Date().toISOString()}${address ? `\nAddress: ${address}` : ''}`
  return (
    <Section title={<>Address{problem ? <Pill tone="warn">Not working</Pill> : address ? <Pill tone="ok">On</Pill> : null}</>} className={problem ? 'border-warning/45' : undefined}>
      {problem ? (
        <div role="alert" data-testid="remote-problem" className="space-y-2 rounded-md border border-warning/40 bg-warning-soft p-3 text-[13px]">
          <b className="block text-text">People cannot sign in from the web right now</b>
          <p className="text-text-muted">The web address may not be reaching this server. You can still use orch here. Nothing was lost.</p>
          <p className="text-text-muted">"Copy details for IT" copies what orch measured, without guessing a cause.</p>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={onCheck}>Check again</Button>
            <Button size="sm" variant="ghost" onClick={() => void copy('details', details)}>{copied === 'details' ? 'Copied' : 'Copy details for IT'}</Button>
          </div>
        </div>
      ) : (
        <>
          <p className="text-[13px] text-text-muted">People sign in here with a passkey.</p>
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <code className="min-w-0 flex-1 break-all rounded-md border border-border bg-bg px-2.5 py-2 font-mono text-xs" data-testid="remote-address">{address}</code>
            <Button size="sm" variant="outline" onClick={() => void copy('address', address ?? '')}>{copied === 'address' ? 'Copied' : 'Copy'}</Button>
          </div>
        </>
      )}
    </Section>
  )
}

function PasskeysCard({ rows, loading, failed, onAdd, onChanged }: { rows: PasskeyRow[]; loading: boolean; failed: boolean; onAdd: () => void; onChanged: () => void }) {
  const [asking, setAsking] = useState<PasskeyRow | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const phase = useStepUpPhase()
  const only = rows.length === 1
  const remove = async (row: PasskeyRow) => {
    if (busy) return
    setBusy(true); setError(null)
    try { await removePasskey(row.id); setAsking(null); onChanged() }
    catch (e) { setError(e instanceof PasskeyError && e.kind !== 'network' ? null : 'The passkey could not be removed. Nothing was changed.') }
    finally { setBusy(false) }
  }
  return (
    <Section title="Your passkeys">
      <p className="text-[13px] text-text-muted">Each device you signed in on has one.</p>
      {loading && <p className="mt-2 text-[13px] text-text-muted" role="status">Loading…</p>}
      {failed && <p role="alert" className="mt-2 text-[13px] text-danger">The passkeys could not be read. Try again in a moment.</p>}
      {rows.length > 0 && (
        <table className="mt-2.5 w-full text-[13px]">
          <thead><tr className="text-left text-xs font-normal text-text-muted"><th className="py-1 font-normal">Device</th><th className="font-normal">Last used</th><th /></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="border-t border-border align-middle" data-testid="passkey-row">
                <td className="py-2.5 pr-2"><span className="font-semibold">{row.name}</span><span className="block text-xs text-text-muted">Added {when(row.created)}</span></td>
                <td className="pr-2">{when(row.last_used)}</td>
                <td className="text-right">{!only && <Button size="sm" variant="outline" className="text-danger" aria-label={`Remove ${row.name}`} onClick={() => { resetStepUp(); setAsking(row) }} disabled={busy}>Remove</Button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {only && (
        <p className="mt-2.5 text-[13px] text-text-muted" data-testid="only-passkey">
          {rows[0].name} is your only passkey. Add another before you could ever remove it. <Button size="sm" className="ml-1.5" onClick={onAdd}>Add a device</Button>
        </p>
      )}
      {asking && (
        <div role="group" aria-label={`Remove ${asking.name}`} className="mt-3 space-y-2 rounded-md border border-danger/40 bg-danger-soft p-3 text-[13px]" data-testid="remove-review">
          <b className="block text-text">Remove the passkey "{asking.name}"?</b>
          <p>The device can no longer sign in, and its open sessions end now. Removed: passkey "{asking.name}". Still able to sign in: {rows.filter((r) => r.id !== asking.id).map((r) => `"${r.name}"`).join(', ')}. The last passkey can never be removed.</p>
          <StepUpNote />
          {error && <p role="alert" className="text-danger">{error}</p>}
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={() => { resetStepUp(); setAsking(null); setError(null) }} disabled={busy}>Keep it</Button>
            <Button size="sm" variant="outline" className="text-danger" onClick={() => void remove(asking)} disabled={busy} aria-busy={busy}>
              <Fingerprint aria-hidden />{needsTouch() ? (phase === 'cancelled' || phase === 'failed' ? 'Try again' : 'Touch your passkey to remove') : 'Remove'}
            </Button>
          </div>
        </div>
      )}
    </Section>
  )
}

const LIFE = 'Whoever opens the link below can add a device that signs in as you. Send it only to yourself. It works once.'

function AddDeviceCard({ count, onUsed }: { count: number; onUsed: () => void }) {
  const [link, setLink] = useState<(EnrollLink & { at: number; before: number }) | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const phase = useStepUpPhase()
  const left = link ? Math.max(0, Math.round((link.at + link.expiresInS * 1000 - now) / 1000)) : 0
  const used = link !== null && count > link.before
  const expired = link !== null && left === 0 && !used

  useEffect(() => {
    if (!link || used || left === 0) return
    const t = setInterval(() => { setNow(Date.now()); onUsed() }, 4000)
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => { clearInterval(t); clearInterval(tick) }
  }, [link, used, left === 0]) // eslint-disable-line react-hooks/exhaustive-deps
  const qr = useMemo(() => (link && !used && !expired ? qrPath(link.link) : null), [link, used, expired])

  const make = async () => {
    if (busy) return
    setBusy(true); setError(null); setCopied(false)
    try {
      const got = await createEnrollLink()
      setLink({ ...got, at: Date.now(), before: count }); setNow(Date.now())
    } catch (e) {
      setError(e instanceof PasskeyError ? null : 'The link could not be made. Nothing was changed.')
    } finally { setBusy(false) }
  }
  const state = link === null ? 'idle' : used ? 'used' : expired ? 'expired' : 'live'
  return (
    <Section title="Add a device">
      <p className="text-[13px] text-text-muted">{LIFE}</p>
      {state === 'idle' && <p className="mt-1.5 text-[13px] text-text-muted">{needsTouch() ? 'Click "Add a device" and touch your passkey. Then the link and QR code appear.' : 'Click "Add a device". Then the link and QR code appear.'}</p>}
      <div className="mt-2.5"><StepUpNote /></div>
      {error && <p role="alert" className="mt-2 text-[13px] text-danger">{error}</p>}
      {state === 'live' && link && qr && (
        <div className="mt-3 flex flex-wrap items-center gap-4" data-testid="enroll-link">
          <svg role="img" aria-label="Link as a QR code" viewBox={`0 0 ${qr.size} ${qr.size}`} className="size-32 shrink-0 rounded-lg bg-white p-1" shapeRendering="crispEdges"><path d={qr.d} fill="#000" /></svg>
          <div className="min-w-[200px] flex-1 space-y-2">
            <code className="block break-all rounded-md border border-border bg-bg px-2.5 py-2 font-mono text-xs">{link.link}</code>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={() => void copyText(link.link).then((ok) => setCopied(ok))}>{copied ? 'Copied' : 'Copy link'}</Button>
              <span className="rounded-full bg-warning-soft px-2.5 py-px text-xs text-warning" data-testid="link-expiry">Expires in {Math.floor(left / 60)} min {String(left % 60).padStart(2, '0')} sec</span>
            </div>
          </div>
        </div>
      )}
      {state === 'used' && <p role="status" className="mt-3 rounded-md border border-brand/40 bg-brand-soft px-3 py-2 text-[13px]" data-testid="link-used"><b className="text-text">Link used.</b> A new device was added. Create a new link to add another.</p>}
      {state === 'expired' && <p role="status" className="mt-3 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-[13px]" data-testid="link-expired"><b className="text-text">Link expired.</b> It was not used. Create a new link when you are ready.</p>}
      <div className="mt-3">
        <Button size="sm" variant={state === 'idle' ? 'default' : 'outline'} onClick={() => void make()} disabled={busy} aria-busy={busy}>
          <Fingerprint aria-hidden />{state === 'idle' ? 'Add a device' : 'Create a new link'}{needsTouch() && phase !== 'idle' && phase !== 'waiting' ? ' (touch again)' : ''}
        </Button>
        {needsTouch() && <span className="ml-2 text-xs text-text-muted">{TOUCH_LABEL.replace(' to confirm', '')} first</span>}
      </div>
    </Section>
  )
}

function LogoutCard() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const go = async () => {
    setBusy(true); setError(null)
    try { await logoutEverywhere(); clearSession(true) }
    catch { setError(PASSKEY_WORDS.network); setBusy(false) }
  }
  return (
    <Section title="Log out everywhere">
      <p className="text-[13px] text-text-muted">Signs out every browser, including this one. Passkeys stay.</p>
      {error && <p role="alert" className="mt-2 text-[13px] text-danger">{error}</p>}
      <div className="mt-2.5"><Button size="sm" variant="outline" className="text-danger" onClick={() => void go()} disabled={busy}>Log out everywhere</Button></div>
    </Section>
  )
}
