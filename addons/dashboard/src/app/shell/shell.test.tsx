import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { createAppRouter } from '../router'

function renderApp(path = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createAppRouter(path)} />
    </QueryClientProvider>,
  )
}

describe('app shell', () => {
  it('renders nav links, the addon group with badges, and the Today placeholder', async () => {
    renderApp('/')
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Board/ })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Code reviews/ })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /Apps & shares/ })).toBeInTheDocument()
    expect((await screen.findAllByRole('img', { name: /From addon:/ })).length).toBeGreaterThanOrEqual(5)
    expect(await screen.findByText('agents granted until 18:00')).toBeInTheDocument()
  })

  it('opens the command palette with Ctrl+K and lists actions and addon commands', async () => {
    const user = userEvent.setup()
    renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    await user.keyboard('{Control>}k{/Control}')
    expect(await screen.findByPlaceholderText(/Search tickets/)).toBeInTheDocument()
    expect(await screen.findByText('New ticket', { selector: '[cmdk-item] *, [cmdk-item]' })).toBeInTheDocument()
    expect(await screen.findByText('Refresh pull requests')).toBeInTheDocument()
  })

  it('toggles the sidebar between wide and narrow with the button and with [', async () => {
    const user = userEvent.setup()
    renderApp('/')
    await screen.findByRole('heading', { name: 'Today' })
    const aside = document.querySelector('aside')!
    const start = aside.getAttribute('data-collapsed')
    await user.click(screen.getByRole('button', { name: /(Expand|Collapse) sidebar/ }))
    expect(aside.getAttribute('data-collapsed')).not.toBe(start)
    await user.keyboard('[[')
    expect(aside.getAttribute('data-collapsed')).toBe(start)
  })

  it('renders an addon page with the badge in the page title', async () => {
    renderApp('/addon/usage/overview')
    const title = await screen.findByRole('heading', { level: 1, name: /Usage/ })
    expect(title.querySelector('[aria-label="From addon: usage"]')).not.toBeNull()
    expect(await screen.findByText('CHF 31.40')).toBeInTheDocument()
  })
})
