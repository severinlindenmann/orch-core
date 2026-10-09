import { useQueryClient } from '@tanstack/react-query'
import { Check, Copy, Fingerprint, ShieldCheck } from 'lucide-react'
import { useRef, useState, type ReactNode } from 'react'
import { toast } from 'sonner'
import { toastApiError } from '@/app/toast'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

// Simulated sensor wait. Zero under vitest: 600 ms of real sleep per signing tipped the 5 s test timeout under parallel load.
export const TOUCH_ID_MS = import.meta.env.MODE === 'test' ? 0 : 600

/**
 * Touch ID simulation for signatures that are not bound to a ticket (grants, workspace settings).
 * Waits for the "sensor", runs the request, refetches everything and reports through a toast.
 * Returns true when the request went through.
 */
export function useSignedAction() {
  const qc = useQueryClient()
  /** `onError`: the caller shows a refusal itself (in place); without it the refusal is a toast. */
  return async (title: string, run: () => Promise<unknown>, onError?: (e: unknown) => void): Promise<boolean> => {
    const id = toast.loading('Touch the sensor to confirm')
    await new Promise((r) => setTimeout(r, TOUCH_ID_MS))
    try {
      const result = await run()
      await qc.invalidateQueries()
      // A run that returns a sentence (an addon action's message) is the one success toast; otherwise the default.
      toast.success(typeof result === 'string' ? result : `${title}: signed with Touch ID`, { id })
      return true
    } catch (e) {
      if (onError) {
        toast.dismiss(id)
        onError(e)
      } else toastApiError(e, 'Could not sign', id)
      return false
    }
  }
}

/** The one sentence every signing dialog puts under its buttons: how the person confirms. */
export function ConfirmHelper() {
  return <p className="text-[12px] text-text-muted sm:text-right">You confirm with Touch ID or your key.</p>
}

/** Technical facts (the hash, what it covers) stay one click away, never at the first level. */
export function SignDetails({ hash, covers }: { hash?: string; covers?: string[] }) {
  const [done, setDone] = useState(false)
  if (!hash && !covers?.length) return null
  return (
    <details className="rounded-md border border-border bg-bg px-3 py-2 text-[12px] text-text-muted">
      <summary className="cursor-pointer select-none text-[12px] text-text-muted hover:text-text">Details</summary>
      <div className="mt-2 space-y-2">
        {covers && covers.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-4">
            {covers.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        )}
        {hash && (
          <div className="flex items-start gap-1">
            <code className="min-w-0 flex-1 break-all font-mono text-text">{hash}</code>
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Copy hash"
              onClick={() => {
                void navigator.clipboard?.writeText(hash).then(() => {
                  setDone(true)
                  setTimeout(() => setDone(false), 1500)
                }, () => {})
              }}
            >
              {done ? <Check /> : <Copy />}
            </Button>
          </div>
        )}
      </div>
    </details>
  )
}

/**
 * Core-rendered signing prompt: what is covered, then "Sign with Touch ID". Render it only while a
 * signature is pending. It closes on Sign (the modal would hide the page from assistive tech) and the
 * caller runs `useSignedAction`, so progress and the result appear as a toast. Only core shows this
 * prompt; an agent or an addon cannot sign.
 */
export function SignPrompt({
  title,
  description = 'Signed with your own key. Only core shows this prompt; an agent or an addon cannot sign it.',
  covers,
  children,
  destructive,
  confirmLabel = 'Sign with Touch ID',
  hash,
  disabled,
  onSign,
  onClose,
}: {
  title: string
  description?: string
  covers: string[]
  children?: ReactNode
  destructive?: boolean
  /** Button text; the default says what happens (Touch ID). Use a verb for the action being signed. */
  confirmLabel?: string
  /** The hash being signed; shown only inside the closed Details. */
  hash?: string
  /** The sign button is off (something blocks what would be signed; say why in `children`). */
  disabled?: boolean
  onSign: () => void
  onClose: () => void
}) {
  const cancel = useRef<HTMLButtonElement>(null)
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="max-w-lg gap-4 border-border bg-surface"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          cancel.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ShieldCheck className="size-4 text-brand" />
            {title}
          </DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        {children}

        <dl className="grid grid-cols-[88px_1fr] gap-x-3 rounded-md border border-border bg-bg p-3 text-[13px]">
          <dt className="text-text-muted">Covers</dt>
          <dd>
            <ul className="list-disc space-y-0.5 pl-4">
              {covers.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </dd>
        </dl>
        <SignDetails hash={hash} />

        <DialogFooter className="gap-2">
          <Button ref={cancel} variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={destructive ? 'destructive' : 'default'} disabled={disabled} onClick={onSign}>
            <Fingerprint />
            {confirmLabel}
          </Button>
        </DialogFooter>
        <ConfirmHelper />
      </DialogContent>
    </Dialog>
  )
}
