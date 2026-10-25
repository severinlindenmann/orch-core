// Add this device (variant A: one card, one button; #1198 R1-8), opened from the one-time link (`/#enroll=<code>`).
// The device name is set for the person (never typed). The code lives in memory and the address bar until used.
import { Check, Fingerprint } from 'lucide-react'
import { useRef, useState } from 'react'
import { deviceName, enrollDevice, PasskeyError, passkeysSupported, type PasskeyFailure } from '@/api/passkey'
import { openSession } from '@/api/bootstrap'
import { finishEnroll, getEnrollCode } from '@/api/enrollLink'
import { Button } from '@/components/ui/button'
import { AuthCard, AuthPage, Badge } from './AuthCard'

type Step = 'ready' | 'waiting' | 'done' | 'used' | 'unsupported' | 'failed' | 'cancelled' | 'locked'

export function EnrollPage({ onDone }: { onDone?: () => void }) {
  const [step, setStep] = useState<Step>(passkeysSupported() ? 'ready' : 'unsupported')
  const [copied, setCopied] = useState(false)
  const session = useRef<string | null>(null) // the new session's bearer, opened when the person clicks "Open orch"
  const busy = useRef(false)
  const name = deviceName()

  const go = () => {
    const code = getEnrollCode()
    if (busy.current || !code) return
    busy.current = true
    setStep('waiting')
    enrollDevice(code, name).then(
      (token) => { session.current = token; finishEnroll(); setStep('done') },
      (e) => setStep(stepOf(e instanceof PasskeyError ? e.kind : 'failed')),
    ).finally(() => { busy.current = false })
  }
  const open = () => {
    const token = session.current
    session.current = null
    if (token) openSession(token)
    onDone?.()
  }
  const toSignIn = () => { finishEnroll(); onDone?.() }
  const copyDetails = () => {
    const text = `orch: this device cannot create a passkey (${deviceName(navigator.userAgent)}; PublicKeyCredential ${typeof PublicKeyCredential === 'undefined' ? 'missing' : 'present'}; secure page ${window.isSecureContext}).`
    void navigator.clipboard?.writeText(text).then(() => setCopied(true), () => {})
  }

  if (step === 'waiting') return (
    <AuthPage><AuthCard title="Confirm on this device" lead="Use the fingerprint, face or PIN prompt that just opened.">
      <div className="mt-4"><Badge tone="wait"><Fingerprint className="size-7" /></Badge></div>
      <p role="status" className="text-sm text-text-muted">Waiting for your device…</p>
    </AuthCard></AuthPage>
  )
  if (step === 'done') return (
    <AuthPage><AuthCard title="This device is ready" lead='Next time, just click "Sign in with your passkey". We recommend adding a second device as a backup.'>
      <div className="mt-4"><Badge tone="ok"><Check className="size-7" /></Badge></div>
      <Button className="mt-1 h-11 w-full" onClick={open}>Open orch</Button>
      <p className="mt-3 text-[13px] text-text-muted">Tip: in Settings, "Add a device" sets up a second one as a backup.</p>
    </AuthCard></AuthPage>
  )
  if (step === 'used' || !getEnrollCode()) return (
    <AuthPage><AuthCard title="This link no longer works" lead="Links work once and for 15 minutes. Ask your IT contact, or a teammate who is already in, for a new one.">
      <div className="mt-4"><Badge tone="bad">!</Badge></div>
      <Button className="h-11 w-full" onClick={toSignIn}>Go to sign in</Button>
    </AuthCard></AuthPage>
  )
  if (step === 'unsupported') return (
    <AuthPage><AuthCard title="This device cannot make a passkey" lead="Your browser or device has no fingerprint, face or PIN set up. Turn on a screen lock for this device and reload, or open the link on another device. The link works for 15 minutes after it was made.">
      <div className="mt-4"><Badge tone="bad">!</Badge></div>
      <Button className="h-11 w-full" onClick={() => location.reload()}>Reload this page</Button>
      <Button variant="ghost" className="mt-2 h-10 w-full" onClick={copyDetails}>{copied ? 'Copied' : 'Copy details for IT'}</Button>
    </AuthCard></AuthPage>
  )
  const note = step === 'failed' ? 'Your device did not confirm. Nothing was changed. Try again.' : step === 'cancelled' ? 'You cancelled. Nothing was changed. Try again when you are ready.' : step === 'locked' ? 'Too many tries. Wait a minute, then try again.' : null
  return (
    <AuthPage><AuthCard title="Add this device" lead="This sets up your fingerprint, face or PIN as your key to orch on this device.">
      <dl className="mt-3.5 grid grid-cols-[100px_1fr] gap-x-2.5 gap-y-1.5 text-left text-[13px]">
        <dt className="text-text-muted">This device</dt><dd>{name}</dd>
        <dt className="text-text-muted">Link works</dt><dd>Once, for 15 minutes</dd>
      </dl>
      {note && <p role="alert" className="mt-3 rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-left text-[13px]">{note}</p>}
      <Button className="mt-[18px] h-11 w-full" onClick={go}><Fingerprint aria-hidden />{note ? 'Try again' : 'Add this device'}</Button>
      <p className="mt-3 text-[13px] text-text-muted">Your device will ask for your fingerprint, face or PIN. Nothing is sent that could be used on another site.</p>
    </AuthCard></AuthPage>
  )
}

function stepOf(kind: PasskeyFailure): Step {
  return kind === 'expired' ? 'used' : kind === 'unsupported' ? 'unsupported' : kind === 'cancelled' ? 'cancelled' : kind === 'locked' ? 'locked' : 'failed'
}
