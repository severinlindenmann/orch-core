import { lazy, Suspense } from 'react'
import { Skeleton } from '@/components/ui/skeleton'
import type { NodeOf } from './nodes'

// recharts loads on first use, so it stays out of the main bundle.
const View = lazy(() => import('./AddonChartView').then((m) => ({ default: m.AddonChartView })))

export function AddonChart({ node }: { node: NodeOf<'chart'> }) {
  return (
    <Suspense fallback={<Skeleton className="h-[200px] w-full" />}>
      <View node={node} />
    </Suspense>
  )
}
