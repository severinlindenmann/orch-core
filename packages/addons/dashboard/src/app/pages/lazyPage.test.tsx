import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Suspense } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { ErrorBoundary, PageProblem } from '@/components/ErrorBoundary'
import { lazyPage, retryFailedPageLoads } from './lazyPage'

describe('lazyPage', () => {
  it('fetches the chunk again when the page boundary is reloaded after a failed load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const load = vi.fn<() => Promise<{ Page: () => React.ReactElement }>>()
    load.mockRejectedValueOnce(new Error('chunk failed')).mockResolvedValue({ Page: () => <h1>Loaded page</h1> })
    const Page = lazyPage(load, 'Page')
    render(
      <ErrorBoundary fallback={(retry) => <PageProblem retry={() => { retryFailedPageLoads(); retry() }} />}>
        <Suspense fallback="loading">
          <Page />
        </Suspense>
      </ErrorBoundary>,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Reload' }))
    expect(await screen.findByRole('heading', { name: 'Loaded page' })).toBeInTheDocument()
    expect(load).toHaveBeenCalledTimes(2)
  })
})
