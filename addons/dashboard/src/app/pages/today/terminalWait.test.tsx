import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import type { NeedsYouItem } from '@/api/types'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 6000 }
const wait: NeedsYouItem = { kind: 'terminal_wait', ticket: 'DEMO-0043', title: 'Pairing flow', text: 'Agent may need you: check its terminal', since: '2026-10-26T09:21:00Z', ref: 'r_01J9Z8K3M4N5P6Q7R8S9T0V1W2' }

describe('Today: "Agent may need you: check its terminal"', () => {
  afterEach(() => {
    mockStore.terminalWaits = []
  })

  it('lists the hint with an Open terminal button, and the header chip opens the same run', async () => {
    const opened: string[] = []
    const on = (e: Event) => opened.push((e as CustomEvent<string>).detail)
    window.addEventListener('orch:dock-open', on)
    const { user } = renderApp('/', { viewer: 'p_sev', setup: (s) => { s.terminalWaits = [wait] } })
    const row = await screen.findByTestId('card-terminal_wait:DEMO-0043', {}, T)
    expect(within(row).getByText('Agent may need you: check its terminal')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Open terminal' }))
    expect(opened).toEqual([wait.ref])
    const chip = await screen.findByTestId('terminal-wait-chip', {}, T)
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
