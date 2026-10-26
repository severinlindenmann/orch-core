import { screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

it('shows Focus only when enabled and links to the ticket session', async () => {
  const { user, router } = renderApp('/ticket/DEMO-0042', { setup: (store) => installAndGrant(store, store.workspaces[0].id, 'focus') })
  const button = await screen.findByTestId('focus-button')
  await user.click(button)
  expect(router.state.location.pathname).toBe('/focus/DEMO-0042')
})

it('hides Focus when disabled', async () => {
  renderApp('/ticket/DEMO-0042')
  await screen.findByTestId('ticket-header')
  expect(screen.queryByTestId('focus-button')).toBeNull()
})
