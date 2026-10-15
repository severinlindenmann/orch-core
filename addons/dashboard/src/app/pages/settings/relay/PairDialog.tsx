import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import type { RelayState, Workspace } from '@/api/types'
import { SignPrompt, useSignedAction } from '@/components/sign/SignPrompt'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { toastApiError } from '@/app/toast'
import { Mono, Pill } from '../../ticket/shared'
import { MockQr } from './MockQr'

/** Seconds left on the pairing code, ticking locally from the host's clock at fetch time. */
function useRemaining(relay: RelayState, fetchedAt: number): number {
  const [tick, setTick] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  if (!relay.pairing) return 0
  const now = Date.parse(relay.now) + (tick - fetchedAt)
  return Math.max(0, Math.ceil((Date.parse(relay.pairing.expires_at) - now) / 1000))
}
/** The pairing universal link's host (shown without its fragment, which holds the offer). */
const RELAY_HOST = 'https://relay.dev.severin.io'
const mmss = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
const spaced = (code: string) => `${code.slice(0, 3)} ${code.slice(3)}`

/**
 * Pair a device (orch v2 D6, §7): a single-use code valid 10 minutes; the phone joins through the relay; both screens
 * show the same 6 characters; the owner confirms here (signed) and the workspace key is sealed to the new device.
 * The QR and the phone are simulated, and say so.
 */
export function PairDialog({ workspace, relay, fetchedAt, onClose }: { workspace: Workspace; relay: RelayState; fetchedAt: number; onClose: () => void }) {
  const qc = useQueryClient()
  const signed = useSignedAction()
  const [signing, setSigning] = useState(false)
  const remaining = useRemaining(relay, fetchedAt)
  const p = relay.pairing
  const expired = !!p && (p.state === 'expired' || remaining === 0)
  const refresh = () => qc.invalidateQueries({ queryKey: ['relay', workspace.id] })

  useEffect(() => {
    if (p && p.state !== 'expired' && remaining === 0) void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remaining === 0])

  const post = async (req: Parameters<typeof api.postRelay>[1], err: string) => {
    try {
      await api.postRelay(workspace.id, req)
      await refresh()
    } catch (e) {
      toastApiError(e, err)
    }
  }
  const cancel = async () => {
    if (p && !expired) await post({ op: 'pair.cancel' }, 'Could not cancel')
    onClose()
  }
  const scan = async () => {
    try {
      await api.simulateRelay(workspace.id, { op: 'scan' })
      await refresh()
    } catch (e) {
      toastApiError(e, 'The simulated phone could not join')
    }
  }

  if (signing && p?.state === 'confirm')
    return (
      <SignPrompt
        title={`Add ${p.label} to ${workspace.name}`}
        description="The device gets this workspace's key. Only core shows this prompt; an agent or an addon cannot sign it."
        covers={[`Device: ${p.label} · pairing ${p.id}`, `Code on both screens: ${spaced(p.fingerprint!)} (${p.fingerprint})`, `Seals the epoch ${relay.epoch} key to it`, 'Scopes: Look, Decide, Operate, Type']}
        confirmLabel="Sign and add device"
        onClose={() => setSigning(false)}
        onSign={() => {
          setSigning(false)
          void signed(`Added ${p.label}`, async () => {
            await api.postRelay(workspace.id, { op: 'pair.confirm', pairing: p.id, fingerprint: p.fingerprint! })
            onClose()
            return `${p.label} is paired. Its key goes out through the relay.`
          })
        }}
      />
    )

  return (
    <Dialog open onOpenChange={(o) => !o && void cancel()}>
      <DialogContent className="max-w-md gap-4 border-border bg-surface">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            Pair a device
            <Pill tone="info">Simulated</Pill>
          </DialogTitle>
          <DialogDescription>A phone or tablet joins {workspace.name} by scanning this code. The first time, it also becomes one of your devices.</DialogDescription>
        </DialogHeader>

        {!p || expired ? (
          <div className="space-y-3 text-[13px]">
            <p role="alert" className="text-text-muted">
              This code expired. Each code works once and for 10 minutes.
            </p>
            <Button onClick={() => void post({ op: 'pair.start' }, 'Could not make a code')}>Make a new code</Button>
          </div>
        ) : p.state === 'waiting' ? (
          <div className="space-y-3 text-[13px]">
            <div className="flex items-start gap-4">
              <MockQr seed={p.id} />
              <div className="space-y-2">
                <p>
                  Valid for <span className="font-mono tabular-nums" aria-label={`${Math.ceil(remaining / 60)} minutes`}>{mmss(remaining)}</span>, once.
                </p>
                <ol className="list-decimal space-y-1 pl-4 text-text-muted">
                  <li>Scan it in the orch app on the iPhone.</li>
                  <li>Both screens show the same 6 characters.</li>
                  <li>You confirm here.</li>
                </ol>
                <p className="text-[12px] text-text-muted">
                  Or open the link on the iPhone: <Mono className="break-all text-[11px]">{RELAY_HOST}/pair#…</Mono>
                </p>
              </div>
            </div>
            <p className="text-[12px] text-text-faint">
              Mock QR: it encodes nothing. The real code holds the link with a single-use offer after the #; that part never leaves the device, so the relay never sees it.
            </p>
            <div className="flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2">
              <span className="flex-1 text-[12px] text-text-muted">Simulation: no phone is needed.</span>
              <Button variant="outline" size="sm" onClick={() => void scan()}>
                Simulate: a phone scans the code
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-3 text-[13px]">
            <p>
              <span className="font-medium">{p.label}</span> wants to join. Does it show this code?
            </p>
            <p className="rounded-md border border-border bg-bg py-3 text-center font-mono text-2xl tracking-[0.2em]" aria-label={`Code ${p.fingerprint!.split('').join(' ')}`}>
              {spaced(p.fingerprint!)}
            </p>
            <p className="text-[12px] text-text-muted">This Mac is your primary device, so it signs the new device's certificate here. If the codes differ, cancel: someone else may be trying to join.</p>
          </div>
        )}

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => void cancel()}>
            {p?.state === 'confirm' && !expired ? 'Codes differ: cancel' : 'Cancel'}
          </Button>
          {p?.state === 'confirm' && !expired && <Button onClick={() => setSigning(true)}>Codes match</Button>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
