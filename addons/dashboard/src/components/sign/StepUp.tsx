// The passkey touch inside a sign panel (remote dashboard, #1198 R1-8). The review stays on screen in every state; the
// confirm button becomes "Touch your passkey to confirm", and these notes say what happened to the touch in plain words.
import { Fingerprint, X } from 'lucide-react'
import { useSyncExternalStore } from 'react'
import { isRemoteSession } from '@/api/features'
import { onStepUpChange, PASSKEY_WORDS, stepUpPhase, type StepUpPhase } from '@/api/passkey'
import { cn } from '@/lib/utils'

export const TOUCH_LABEL = 'Touch your passkey to confirm'

export function useStepUpPhase(): StepUpPhase {
  return useSyncExternalStore(onStepUpChange, stepUpPhase, () => 'idle')
}

/** Is a touch part of this confirm? Only on a session opened by a passkey sign-in on the web address. */
export const needsTouch = (): boolean => isRemoteSession()

/** What happened to the last touch. Nothing when there is nothing to say. */
export function StepUpNote({ className }: { className?: string }) {
  const phase = useStepUpPhase()
  if (phase === 'idle') return null
  const waiting = phase === 'waiting'
  const tone = waiting ? 'border-brand/40 bg-brand-soft' : phase === 'failed' ? 'border-danger/40 bg-danger-soft' : phase === 'expired' ? 'border-warning/40 bg-warning-soft' : 'border-border-strong bg-surface-2'
  const [title, body] =
    phase === 'waiting' ? ['Touch your passkey now', 'Use the prompt from your device. This confirms exactly the details shown above.']
    : phase === 'cancelled' ? ['You cancelled. Nothing was changed.', 'Check the details above, then try again when you are ready.']
    : phase === 'expired' ? ['Your passkey check ran out (10 minutes)', PASSKEY_WORDS.stale.replace('Your passkey check ran out (10 minutes). ', '')]
    : ['Your device did not confirm', 'Nothing was changed. Try again with the device you signed in with. If it keeps failing, ask your IT contact.']
  return (
    <div role={waiting ? 'status' : 'alert'} data-testid="stepup-note" data-phase={phase} className={cn('flex items-center gap-3 rounded-md border px-3 py-2.5 text-[13px]', tone, className)}>
      <span aria-hidden className={cn('grid size-9 shrink-0 place-items-center rounded-full', waiting ? 'bg-brand text-on-brand motion-safe:animate-pulse' : phase === 'failed' ? 'bg-danger text-bg' : 'bg-surface-3 text-text')}>
        {waiting || phase === 'failed' || phase === 'expired' ? <Fingerprint className="size-5" /> : <X className="size-5" />}
      </span>
      <span><b className="block font-semibold text-text">{title}</b><span className="text-text-muted">{body}</span></span>
    </div>
  )
}
