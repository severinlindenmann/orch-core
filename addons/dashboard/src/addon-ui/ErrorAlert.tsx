import { TriangleAlert, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ActionError } from './useRunAddonAction'

/** Why an action was refused, in place of a toast: stays until the next success or until the person dismisses it. */
export function ErrorAlert({ error, onDismiss, className }: { error: ActionError; onDismiss: () => void; className?: string }) {
  return (
    <div role="alert" className={cn('flex items-start gap-2 rounded-md border border-danger/40 bg-danger-soft px-2.5 py-1.5 text-left text-[12px] text-text', className)}>
      <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-danger" aria-hidden />
      <p className="min-w-0 flex-1 whitespace-normal break-words">
        {error.message}
        {error.hint && <span className="text-text-muted"> {error.hint}</span>}
      </p>
      <button type="button" aria-label="Dismiss" onClick={onDismiss} className="shrink-0 rounded-sm text-text-muted hover:text-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring">
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  )
}
