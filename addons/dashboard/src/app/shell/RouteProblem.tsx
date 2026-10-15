import { useRouter, type ErrorComponentProps } from '@tanstack/react-router'
import { PageProblem } from '@/components/ErrorBoundary'
import { retryFailedPageLoads } from '../pages/lazyPage'

/** A page whose code or loader failed (a chunk that did not load): the page boundary's note, Reload tries again. */
export function RouteProblem({ reset }: ErrorComponentProps) {
  const router = useRouter()
  return (
    <PageProblem
      retry={() => {
        retryFailedPageLoads()
        reset()
        void router.invalidate()
      }}
    />
  )
}
