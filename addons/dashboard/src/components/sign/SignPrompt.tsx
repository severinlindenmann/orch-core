import { useQueryClient } from '@tanstack/react-query'
import { Fingerprint, ShieldCheck } from 'lucide-react'
import type { ReactNode } from 'react'
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
  return async (title: string, run: () => Promise<unknown>): Promise<boolean> => {
    const id = toast.loading('Touch the sensor to confirm')
    await new Promise((r) => setTimeout(r, TOUCH_ID_MS))
    try {
      await run()
      await qc.invalidateQueries()
      toast.success(`${title}: signed with Touch ID`, { id })
      return true
    } catch (e) {
      toastApiError(e, 'Could not sign', id)
      return false
    }
  }
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
  /** The sign button is off (something blocks what would be signed; say why in `children`). */
  disabled?: boolean
  onSign: () => void
  onClose: () => void
}) {
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-lg gap-4 border-border bg-surface">
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

        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant={destructive ? 'destructive' : 'default'} disabled={disabled} onClick={onSign}>
            <Fingerprint />
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
