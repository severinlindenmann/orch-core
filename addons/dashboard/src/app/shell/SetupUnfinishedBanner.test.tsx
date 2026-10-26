import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test } from 'vitest'
import { oneClickUnfinished, SetupUnfinishedLine } from './SetupUnfinishedBanner'

test('unfinished means the flag is on and the key is not in one-click custody', () => {
  expect(oneClickUnfinished({ custody: 'passphrase', enabled: true, available: false })).toBe(true)
  expect(oneClickUnfinished({ custody: 'convenience', enabled: true, available: true })).toBe(false)
  expect(oneClickUnfinished({ custody: 'passphrase', enabled: false, available: false })).toBe(false)
  expect(oneClickUnfinished(null)).toBe(false)
  expect(oneClickUnfinished(undefined)).toBe(false)
})

test('the banner says "Finish turning on" and the dialog points to the passphrase switch, not orch setup', async () => {
  render(<SetupUnfinishedLine />)
  expect(screen.getByTestId('setup-unfinished-banner')).toHaveTextContent('One click needs one more step')
  await userEvent.click(screen.getByRole('button', { name: 'Finish turning on' }))
  expect(await screen.findByText(/type your passphrase in the dialog/)).toBeInTheDocument()
  expect(screen.queryByText('orch setup')).toBeNull()
})
