// Remote sign-in (variant A: a minimal card with one button; #1198 R1-8). The device asks for a fingerprint, face or PIN;
// nothing is typed in orch. On this computer's own address there is no passkey login: the old "open from the terminal"
// words stay (`local`). The way back in (an SSH tunnel) is for IT, behind a fold, never something the person types.
import { Fingerprint } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { HEADLESS_PORT as TUNNEL_PORT, PasskeyError, passkeysSupported, prepareLogin, signIn } from '@/api/passkey'
import { Button } from '@/components/ui/button'
import { AuthCard, AuthPage, Notice } from './AuthCard'

const LOCK_MS = 60_000 // the server's sign-in limit is per minute

type Step = 'checking' | 'local' | 'ready' | 'waiting' | 'failed' | 'cancelled' | 'locked' | 'unreachable' | 'unsupported'

const clock = (at: number) => new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

/**
 * `ended`: a session was open and ended (so say it). `local` is shown when this is not the web address.
 * `contact`: an optional IT contact the server may provide one day; shown in the help only when present (a seam, no
 * server state exists for it yet).
 */
export function LoginGate({ ended = false, local, contact }: { ended?: boolean; local: ReactNode; contact?: string | null }) {
  const [step, setStep] = useState<Step>('checking')
  const [until, setUntil] = useState(0)
  const busy = useRef(false)

  const probe = useCallback(() => {
    setStep('checking')
    prepareLogin().then(
      (remote) => setStep(remote ? (passkeysSupported() ? 'ready' : 'unsupported') : 'local'),
      (e) => lock(e, setStep, setUntil, 'unreachable'),
    )
  }, [])
  useEffect(probe, [probe])

  // A lockout ends by itself: the button comes back at the time shown.
  useEffect(() => {
    if (step !== 'locked') return
    const t = setTimeout(() => setStep('ready'), Math.max(0, until - Date.now()))
    return () => clearTimeout(t)
  }, [step, until])

  const go = () => {
    if (busy.current) return
    busy.current = true
    setStep('waiting')
    signIn().then(
      () => { /* the session is open: the shell takes over */ },
      (e) => lock(e, setStep, setUntil, 'failed'),
    ).finally(() => { busy.current = false })
  }

  if (step === 'local') return <>{local}</>
  if (step === 'checking') return <AuthPage><div role="status" aria-label="Loading" className="size-6 animate-pulse rounded-full bg-surface-3" /></AuthPage>
  const waiting = step === 'waiting'
  const locked = step === 'locked'
  return (
    <AuthPage>
      <AuthCard
        title={locked ? 'Too many tries' : 'Sign in to orch'}
        lead={step === 'ready' || waiting ? 'Use the passkey you set up on this device. Your device asks for your fingerprint, face or screen-lock code.' : undefined}
      >
        {ended && step === 'ready' && <Notice tone="info" title="You were signed out">Sign in again to go on. Nothing was lost.</Notice>}
        {step === 'failed' && <Notice tone="err" title="That did not work">Your device did not confirm. Nothing was changed. Try again, and make sure you use the device you set up orch on.</Notice>}
        {step === 'cancelled' && <Notice tone="info" title="You cancelled">Nothing was changed. Try again when you are ready.</Notice>}
        {locked && <Notice tone="warn" title="Sign-in is paused for a minute">This protects your account. It opens again at {clock(until)}. The page will enable the button by itself.</Notice>}
        {step === 'unreachable' && <Notice tone="err" title="orch did not answer">Check your connection, then try again. If it keeps happening, tell your IT contact.</Notice>}
        {step === 'unsupported' && <Notice tone="err" title="This browser cannot use a passkey">Open this page in a current browser on a device with a fingerprint, face or screen lock.</Notice>}
        {step === 'unreachable' ? (
          <Button className="mt-[18px] h-11 w-full" onClick={probe}>Try again</Button>
        ) : step !== 'unsupported' && (
          <Button className="mt-[18px] h-11 w-full" onClick={go} disabled={waiting || locked} aria-busy={waiting}>
            <Fingerprint aria-hidden />{waiting ? 'Confirm on your device…' : step === 'failed' || step === 'cancelled' ? 'Try again' : 'Sign in with your passkey'}
          </Button>
        )}
        {locked && <p className="mt-3 text-left text-[13px] text-text-muted">Not you? Tell {contact ?? 'your IT contact'}: someone else may be trying to get in.</p>}
        <Help contact={contact} open={step === 'failed'} />
      </AuthCard>
    </AuthPage>
  )
}

function lock(e: unknown, setStep: (s: Step) => void, setUntil: (n: number) => void, otherwise: Step) {
  const kind = e instanceof PasskeyError ? e.kind : 'failed'
  if (kind === 'locked') { setUntil(Date.now() + LOCK_MS); setStep('locked') }
  else if (kind === 'cancelled') setStep('cancelled')
  else if (kind === 'unsupported') setStep('unsupported')
  else if (kind === 'network') setStep(otherwise === 'failed' ? 'unreachable' : otherwise)
  else setStep(otherwise)
}

function Help({ contact, open }: { contact?: string | null; open: boolean }) {
  const host = typeof location === 'undefined' ? 'your-server' : location.hostname
  return (
    <details className="mt-4 border-t border-border pt-3 text-left text-[13px] text-text-muted" open={open || undefined} data-testid="login-help">
      <summary className="cursor-pointer text-text">{open ? 'Still stuck?' : "Can't sign in?"}</summary>
      <p className="mt-2">
        {open ? 'This device may not have your passkey. ' : ''}Ask {contact ?? 'the person who set up orch for you (your IT contact)'}. They can send you a new link for this device.
      </p>
      <details className="mt-3" data-testid="login-for-it">
        <summary className="cursor-pointer text-text">For IT</summary>
        <p className="mt-2">If the website itself is the problem, there is a second, private way in that does not use it. On the person's own computer, open an SSH tunnel to the server:</p>
        <code className="mt-2 block break-all rounded-md border border-border bg-bg px-2.5 py-2 font-mono text-xs text-text">{`ssh -o ExitOnForwardFailure=yes -L ${TUNNEL_PORT}:127.0.0.1:${TUNNEL_PORT} <user>@${host}`}</code>
        <p className="mt-2">On the server, <b className="font-semibold text-text">orch dashboard open --print-url</b> prints a one-time link; open it in the browser on the person's computer. It is the same orch, on the loopback address, where web sign-in is not used.</p>
      </details>
    </details>
  )
}
