import { TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'

/** The calm error state of a page whose data could not be loaded, with Retry. */
export function LoadFailed({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <div className="mx-auto mt-16 max-w-md rounded-lg border border-border bg-surface p-8 text-center" role="alert">
      <TriangleAlert className="mx-auto size-7 text-warning" aria-hidden />
      <h2 className="mt-3 text-lg font-semibold">Could not load {what}</h2>
      <p className="mt-1.5 text-[13px] text-text-muted">Something went wrong while loading. Try again in a moment.</p>
      <Button variant="outline" size="sm" className="mt-4" onClick={onRetry}>
        Retry
      </Button>
    </div>
  )
}
