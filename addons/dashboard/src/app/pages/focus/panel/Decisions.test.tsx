import { screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
const setup = (s: typeof mockStore) => installAndGrant(s, s.workspaces[0].id, 'focus')
it('shows open option buttons, gate decisions and the existing answer dialog', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  const tab = await screen.findByRole('tab', { name: 'Decisions' })
  await user.click(tab)
  const panel = await screen.findByTestId('focus-panel-decisions')
  const q = mockStore.ticket('DEMO-0043')!.questions_state.find(q => q.state === 'open')!
  expect(within(panel).getByText(q.text)).toBeInTheDocument()
  const option = await within(panel).findByRole('button', { name: q.options![0].label })
  await user.click(option)
  expect(await screen.findByRole('dialog')).toBeInTheDocument()
  expect(within(panel).getByText('Requirements')).toBeInTheDocument()
})
it('shows an empty state for scratch', async () => {
  const { user } = renderApp('/focus?scratch=1', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Decisions' }))
  expect(screen.getByText('No ticket yet. Decisions will appear here.')).toBeInTheDocument()
})
it('shows the answered choice and a readable person name', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Decisions' }))
  const panel = await screen.findByTestId('focus-panel-decisions')
  const q = mockStore.ticket('DEMO-0043')!.questions_state.find(q => q.state === 'answered')!
  expect(q).toBeDefined()
  const card = within(panel).getByText(q.text).closest('section')!
  expect(card).toHaveTextContent(q.options?.find(o => o.key === q.answer?.option)?.label ?? q.answer!.text!)
  expect(card).not.toHaveTextContent(q.answer!.by)
})
it('passes a written answer into the existing signing dialog', async () => {
  const { user } = renderApp('/focus/DEMO-0043', { setup })
  await user.click(await screen.findByRole('tab', { name: 'Decisions' }))
  const input = await screen.findByRole('textbox', { name: /Your answer to/ })
  await user.type(input, 'Use the monthly export.')
  await user.click(screen.getByRole('button', { name: 'Send written answer' }))
  expect(await screen.findByRole('dialog')).toHaveTextContent('Use the monthly export.')
})
