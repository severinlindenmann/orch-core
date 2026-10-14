import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { resetLayoutPreview } from './layoutPreview'

const original = { ...api }
afterEach(() => {
  cleanup()
  resetLayoutPreview()
  expect({ ...api }).toEqual(original)
})

it('preview swaps are reversible, including after a second installation', async () => {
  const first = renderApp('/__layout/records-ok')
  await screen.findByTestId('records-export-warning')
  expect(api.getRecords).not.toBe(original.getRecords)
  first.unmount()
  resetLayoutPreview()
  expect({ ...api }).toEqual(original)
  renderApp('/__layout/records-first')
  await screen.findByTestId('records-not-configured')
  expect(api.getRecords).not.toBe(original.getRecords)
})


it.each(['records-push-failed', 'records-push-mismatch'])('preview supplies the current destination for %s', async (scenario) => {
  const { user } = renderApp(`/__layout/${scenario}`)
  await user.click(await screen.findByRole('button', { name: 'Commit and push' }))
  const dialog = await screen.findByRole('dialog')
  const warning = await within(dialog).findByTestId('records-export-warning')
  expect(warning).not.toHaveTextContent('last known')
  expect(dialog).toHaveTextContent('orch trusts your word that only you read')
  if (scenario === 'records-push-mismatch') expect(within(dialog).getByTestId('records-push-mismatch')).toHaveTextContent('This is not where the last push went')
})
