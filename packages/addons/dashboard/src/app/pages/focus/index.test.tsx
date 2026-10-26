import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces[0].id, 'focus')
afterEach(() => vi.restoreAllMocks())

it('renders the ticket key and title with slots and a collapsible tab panel', async () => {
  const { user } = renderApp('/focus/DEMO-0042', { setup })
  const chip = await screen.findByTestId('focus-ticket-chip')
  await waitFor(() => expect(chip).toHaveTextContent(mockStore.ticket('DEMO-0042')!.title))
  expect(chip).toHaveTextContent('DEMO-0042')
  expect(screen.getByTestId('focus-conversation')).toBeInTheDocument()
  expect(screen.queryByTestId('focus-tasks-list')).toBeNull() // Tasks open from the chip, not as a second list
  expect(screen.getByRole('button', { name: 'Stop all' })).toBeDisabled()
  expect(screen.getByRole('status')).toHaveTextContent('No agent running')
  for (const tab of ['Changes', 'Artifacts', 'Decisions', 'Context']) {
    await user.click(screen.getByRole('tab', { name: tab }))
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
  }
  await user.click(screen.getByRole('button', { name: 'Hide panel' }))
  expect(screen.queryByTestId('focus-panel')).toBeNull()
  await user.click(screen.getByRole('button', { name: 'Show panel' }))
  expect(screen.getByTestId('focus-panel')).toBeInTheDocument()
  await user.click(screen.getByRole('link', { name: 'Exit focus' }))
  expect(await screen.findByTestId('ticket-header')).toBeInTheDocument()
})

it('renders scratch with No ticket and opens the existing create form', async () => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  const root = await screen.findByTestId('focus-root')
  expect(within(root).getByText('Scratch session')).toBeInTheDocument()
  expect(screen.queryByTestId('focus-start')).toBeNull()
  expect(screen.getByTestId('focus-conversation')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Create ticket from this' }))
  expect(await screen.findByRole('dialog', { name: 'New ticket' })).toBeInTheDocument()
})

it('hides focus when the addon is off', async () => {
  const { router } = renderApp('/focus/DEMO-0042')
  await waitFor(() => expect(router.state.status).toBe('idle'))
  expect(screen.queryByTestId('focus-root')).toBeNull()
})

it('requests browser fullscreen and exits it', async () => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  const root = await screen.findByTestId('focus-root')
  const request = vi.fn(async () => {
    vi.spyOn(document, 'fullscreenElement', 'get').mockReturnValue(root)
    fireEvent(document, new Event('fullscreenchange'))
  })
  Object.defineProperty(root, 'requestFullscreen', { configurable: true, value: request })
  // jsdom does not implement the browser fullscreen API.
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => null })
  const exit = vi.fn(async () => {
    vi.spyOn(document, 'fullscreenElement', 'get').mockReturnValue(null)
    fireEvent(document, new Event('fullscreenchange'))
  })
  Object.defineProperty(document, 'exitFullscreen', { configurable: true, value: exit })
  const toggle = screen.getByRole('button', { name: 'Fullscreen' })
  await user.click(toggle)
  expect(request).toHaveBeenCalledOnce()
  expect(toggle).toHaveAttribute('aria-pressed', 'true')
  await user.click(toggle)
  expect(exit).toHaveBeenCalledOnce()
  expect(toggle).toHaveAttribute('aria-pressed', 'false')
})

it.each(['missing', 'rejected'])('falls back to a CSS class when fullscreen is %s', async (mode) => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  const root = await screen.findByTestId('focus-root')
  Object.defineProperty(root, 'requestFullscreen', { configurable: true, value: mode === 'missing' ? undefined : vi.fn().mockRejectedValue(new Error('Unavailable')) })
  const toggle = screen.getByRole('button', { name: 'Fullscreen' })
  await user.click(toggle)
  expect(root).toHaveClass('focus-fullscreen', 'fixed', 'inset-0')
  await user.click(toggle)
  expect(root).not.toHaveClass('focus-fullscreen')
  await user.click(toggle)
  await user.keyboard('{Escape}')
  expect(root).not.toHaveClass('focus-fullscreen')
})
