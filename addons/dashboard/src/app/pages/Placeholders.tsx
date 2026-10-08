import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { AddonFrame } from '@/addon-ui/AddonFrame'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../workspace'

export function Placeholder({ title, addon }: { title: string; addon?: string }) {
  const body = <p className="text-sm text-text-muted">Coming in a later iteration.</p>
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      {addon ? <AddonFrame addon={addon} title={title}>{body}</AddonFrame> : body}
    </div>
  )
}

export function TodayPlaceholder() {
  const { workspace } = useWorkspace()
  const { data, isLoading } = useQuery({
    queryKey: ['today', workspace?.id],
    queryFn: () => api.getToday(workspace!.id),
    enabled: !!workspace,
  })
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Today</h1>
      {isLoading || !data ? (
        <Skeleton className="h-24 w-full max-w-xl" />
      ) : (
        <div className="max-w-xl rounded-lg border border-border bg-surface p-4 text-sm text-text-muted">
          <span className="font-semibold text-text">{data.needs_you.length}</span> items need you,{' '}
          <span className="font-semibold text-text">{data.working.length}</span> tickets are being worked on.
        </div>
      )}
    </div>
  )
}
