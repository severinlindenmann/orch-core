import { TriangleAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

/** The loading state of a named read inside a node: calm skeleton rows, announced once. */
export function ReadLoading({ label, rows = 4 }: { label: string; rows?: number }) {
  return (
    <div role="status" aria-label={label} className="space-y-2 p-1.5">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-7 w-full" />
      ))}
    </div>
  )
}

/** The error state of a named read inside a node (small: a node sits in a page, so this is not a full-page error). */
export function ReadFailed({ what, onRetry, detail }: { what: string; onRetry?: () => void; detail?: string }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-md border border-border bg-surface-2 px-3 py-2.5 text-[13px]">
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-medium text-text">Could not load {what}.</p>
        <p className="text-text-muted">{detail ?? 'Try again in a moment.'}</p>
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  )
}
