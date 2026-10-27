import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { NeedsYouItem } from '@/api/types'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 6000 }
const wait: NeedsYouItem = { kind: 'terminal_wait', ticket: 'DEMO-0043', title: 'Pairing flow', text: 'Agent may need you: check its terminal', since: '2026-10-26T09:21:00Z', ref: 'r_01J9Z8K3M4N5P6Q7R8S9T0V1W2' }

describe('Today: "Agent is waiting for you" (plain words, run-13 A10)', () => {
  afterEach(() => {
    mockStore.terminalWaits = []
  })

  it('lists the hint with an Open agent button and no word "terminal", and the header chip opens the same run', async () => {
    const opened: string[] = []
    const on = (e: Event) => opened.push((e as CustomEvent<string>).detail)
    window.addEventListener('orch:dock-open', on)
    const { user } = renderApp('/', { viewer: 'p_sev', setup: (s) => { s.terminalWaits = [wait] } })
    const row = await screen.findByTestId('card-terminal_wait:DEMO-0043', {}, T)
    expect(within(row).getByText('Agent is waiting for you')).toBeInTheDocument()
    expect(row).not.toHaveTextContent(/terminal/i)
    expect(within(row).queryByRole('button', { name: /terminal/i })).toBeNull()
    await user.click(within(row).getByRole('button', { name: 'Open agent' }))
    expect(opened).toEqual([wait.ref])
    const chip = await screen.findByTestId('terminal-wait-chip', {}, T)
    expect(chip).toHaveTextContent('Agent is waiting · Open')
    expect(chip).not.toHaveTextContent(/terminal/i)
    await user.click(chip)
    expect(opened).toEqual([wait.ref, wait.ref])
    window.removeEventListener('orch:dock-open', on)
  })

  it('has no chip and no row when the host sends none', async () => {
    renderApp('/', { viewer: 'p_sev' })
    await screen.findByText(/need you/, {}, T)
    await waitFor(() => expect(screen.queryByTestId('terminal-wait-chip')).toBeNull())
    expect(screen.queryByTestId('card-terminal_wait:DEMO-0043')).toBeNull()
  })
})
