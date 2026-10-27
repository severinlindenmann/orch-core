import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { AccessSelect } from './AccessSelect'

const lengths = [[8, '8 hours'], [24, '1 day']] as const
it('the headline names the length that will be signed', () => {
  render(<AccessSelect hours={24} onHours={() => {}} lengths={lengths} />)
  expect(screen.getByText('Agents may work for you for 1 day.')).toBeInTheDocument()
})
