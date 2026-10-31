import { screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }

// Run 8, 6/6.3/6.6: the Actions menu lists every action, the primary one too (tagged "next"); never "Nothing else needs you".
it('the verdict is the next-step card button and also in Actions, tagged next', async () => {
  const { user } = renderApp('/ticket/DEMO-0041')
  await screen.findByRole('heading', { level: 1 }, T)
  expect(within(screen.getByTestId('next-step')).getByRole('button', { name: 'Give verdict' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: /Actions/ }))
  const item = await screen.findByRole('menuitem', { name: /Give verdict/ })
  expect(within(item).getByTestId('next-tag')).toHaveTextContent('next')
  expect(screen.queryByText(/Nothing else needs you/)).toBeNull()
  expect(screen.getByRole('menuitem', { name: 'Send back' })).toBeInTheDocument()
  expect(screen.queryByText(/signed in your terminal|one click/i)).toBeNull()
})

it('Start agent in Actions is off while an agent works, with the reason', async () => {
  const { user } = renderApp('/ticket/DEMO-0043')
  await screen.findByRole('heading', { level: 1 }, T)
  await user.click(screen.getByRole('button', { name: /Actions/ }))
  expect(screen.getByTestId('agent-status')).toHaveTextContent(/is waiting for you/) // the premise: an agent works here
  const items = await screen.findAllByRole('menuitem', { name: /Start agent/ }, T)
  expect(items).toHaveLength(1) // present, so the checks below cannot pass on an empty list
  expect(items[0]).toHaveAttribute('aria-disabled', 'true')
  expect(items[0]).toHaveTextContent('An agent is already working on this ticket')
})
