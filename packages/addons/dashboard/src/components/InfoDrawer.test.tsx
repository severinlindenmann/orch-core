import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InfoDrawer } from './InfoDrawer'

describe('InfoDrawer', () => {
  it('hides details until the i is pressed', async () => {
    render(<InfoDrawer title="Details"><p>Access id: gr_123</p></InfoDrawer>)
    expect(screen.queryByText(/gr_123/)).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'More information' }))
    expect(await screen.findByText(/gr_123/)).toBeTruthy()
  })
  it('never says Technical details', () => {
    render(<InfoDrawer><p>x</p></InfoDrawer>)
    expect(screen.queryByText(/Technical details/)).toBeNull()
  })
})
