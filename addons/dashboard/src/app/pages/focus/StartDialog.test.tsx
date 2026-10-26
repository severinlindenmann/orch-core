import { screen, waitFor, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'

const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces[0].id, 'focus')

it('shows all three choices inline and starts scratch', async () => {
  const { user, router } = renderApp('/focus', { setup })
  const start = await screen.findByTestId('focus-start')
  expect(within(start).getByRole('heading', { name: 'Start focus session' })).toBeInTheDocument()
  expect(within(start).getByRole('button', { name: 'New ticket with me' })).toBeInTheDocument()
  expect(within(start).getByRole('button', { name: 'Existing ticket' })).toBeInTheDocument()
  await user.click(within(start).getByRole('button', { name: 'No ticket, just work' }))
  await waitFor(() => expect(router.state.location.search).toMatchObject({ scratch: 1 }))
  expect(await screen.findByTestId('focus-conversation')).toBeInTheDocument()
  expect(screen.queryByTestId('focus-start')).toBeNull()
})

it('picks an open ticket and navigates to its session', async () => {
  const { user, router } = renderApp('/focus', { setup })
  await user.click(await screen.findByRole('button', { name: 'Existing ticket' }))
  const picker = await screen.findByRole('combobox', { name: 'Open tickets' })
  for (const option of within(picker).getAllByRole('option')) {
    const key = (option as HTMLOptionElement).value
    if (key) expect(['done', 'closed']).not.toContain(mockStore.ticket(key)?.status)
  }
  await user.selectOptions(picker, 'DEMO-0043')
  await waitFor(() => expect(router.state.location.pathname).toBe('/focus/DEMO-0043'))
  await waitFor(() => expect(screen.getByTestId('focus-ticket-chip')).toHaveTextContent('DEMO-0043'))
})

it('creates a ticket through the existing form then opens its focus session', async () => {
  const { user, router } = renderApp('/focus', { setup })
  await user.click(await screen.findByRole('button', { name: 'New ticket with me' }))
  const dialog = await screen.findByRole('dialog', { name: 'New ticket' })
  await user.type(await within(dialog).findByLabelText('Title'), 'Focus entry ticket')
  await user.type(within(dialog).getByLabelText(/^What should be true/), 'Keep the work in focus')
  await user.click(within(dialog).getByRole('button', { name: 'Create' }))
  await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/focus\/DEMO-\d+$/))
  const key = router.state.location.pathname.split('/').at(-1)!
  expect(mockStore.ticket(key)?.title).toBe('Focus entry ticket')
  await waitFor(() => expect(screen.getByTestId('focus-ticket-chip')).toHaveTextContent('Focus entry ticket'))
})
