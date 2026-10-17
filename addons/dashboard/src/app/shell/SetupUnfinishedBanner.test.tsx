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

test('the banner says what is unfinished and Finish setup names the command', async () => {
  render(<SetupUnfinishedLine />)
  expect(screen.getByTestId('setup-unfinished-banner')).toHaveTextContent('Setup unfinished: one-click is not on yet')
  await userEvent.click(screen.getByRole('button', { name: 'Finish setup' }))
  expect(await screen.findByText('orch setup')).toBeInTheDocument()
})
