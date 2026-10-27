import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InfoDrawer } from './InfoDrawer'

describe('InfoDrawer', () => {
  it('hides details until the i is pressed', async () => {
    render(<InfoDrawer title="Details"><p>Access id: gr_123</p></InfoDrawer>)
    expect(screen.queryByText(/gr_123/)).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'More information' }))
    expect(await screen.findByText(/gr_123/)).toBeTruthy()
  })
  it('an action inside the drawer closes it when its flow opens', async () => {
    const onOpenChange = vi.fn()
    function Host() {
      const [flow, setFlow] = useState(false)
      return <>
        <InfoDrawer onOpenChange={onOpenChange}>{(close) => <button type="button" onClick={() => { close(); setFlow(true) }}>Start flow</button>}</InfoDrawer>
        {flow && <p>Flow open</p>}
      </>
    }
    render(<Host />)
    await userEvent.click(screen.getByRole('button', { name: 'More information' }))
    expect(onOpenChange).toHaveBeenLastCalledWith(true)
    await userEvent.click(await screen.findByRole('button', { name: 'Start flow' }))
    expect(screen.getByText('Flow open')).toBeTruthy()
    await waitFor(() => expect(screen.queryByTestId('info-drawer-content')).toBeNull())
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
  })
  it('never says Technical details', () => {
    render(<InfoDrawer><p>x</p></InfoDrawer>)
    expect(screen.queryByText(/Technical details/)).toBeNull()
  })
})
