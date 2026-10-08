import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { createAppRouter } from './router'

describe('app shell', () => {
  it('renders the sidebar with nav links, addon group badges and the Today placeholder', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={createAppRouter('/')} />
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Board/ })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Pull requests/ })).toBeInTheDocument()
    expect((await screen.findAllByRole('img', { name: /From addon:/ })).length).toBeGreaterThanOrEqual(3)
  })
})
