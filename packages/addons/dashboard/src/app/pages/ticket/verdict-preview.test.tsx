import { screen, within } from '@testing-library/react'
import { expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }

// Run 8, 7.4: the preview replaces the dialog body; "Back to verdict" returns with the choice kept; signing is off meanwhile.
it('Show preview swaps the verdict body for the preview; Back keeps the choice; the sign button is off while previewing', async () => {
  const { user } = renderApp('/ticket/DEMO-0041')
  await screen.findByRole('heading', { level: 1 }, T)
  await user.click(await screen.findByRole('button', { name: /Give verdict/ }, T))
  const dialog = await screen.findByRole('dialog', {}, T)
  await user.click(within(dialog).getByRole('radio', { name: /^Pass on/ }))
  const sign = within(dialog).getByRole('button', { name: 'Pass' })
  expect(sign).toBeEnabled()
  expect(within(dialog).getByTestId('verdict-commit')).toBeInTheDocument()
  await user.click(within(dialog).getAllByRole('button', { name: /^Show preview of/ })[0])
  expect(await within(dialog).findByRole('button', { name: 'Back to verdict' })).toBeInTheDocument()
  expect(within(dialog).queryByRole('radio')).toBeNull()
  expect(within(dialog).getByRole('button', { name: 'Pass' })).toBeDisabled()
  await user.click(within(dialog).getByRole('button', { name: 'Back to verdict' }))
  expect(within(dialog).getByRole('radio', { name: /^Pass on/ })).toBeChecked()
  expect(within(dialog).getByRole('button', { name: 'Pass' })).toBeEnabled()
})
