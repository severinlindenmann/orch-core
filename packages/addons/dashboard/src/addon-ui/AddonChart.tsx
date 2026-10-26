import { Suspense } from 'react'
import { lazyWithPreload } from '@/lib/lazyPreload'
import { Skeleton } from '@/components/ui/skeleton'
import type { NodeOf } from './nodes'

// recharts loads on first use, so it stays out of the main bundle.
export const AddonChartChunk = lazyWithPreload(() => import('./AddonChartView').then((m) => ({ default: m.AddonChartView })))

export function AddonChart({ node }: { node: NodeOf<'chart'> }) {
  return (
    <Suspense fallback={<Skeleton className="h-[200px] w-full" />}>
      <AddonChartChunk node={node} />
    </Suspense>
  )
}
