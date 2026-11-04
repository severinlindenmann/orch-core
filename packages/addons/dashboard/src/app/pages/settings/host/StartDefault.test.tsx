import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { agentPrefKey, readPref } from '../../new-ticket/useStartPlan'
import { StartDefault } from './Grants'

afterEach(() => { cleanup(); localStorage.clear() })

describe('the start default under Agent access', () => {
  it('writes the same per-person, per-workspace choice the New ticket button reads', async () => {
    const key = agentPrefKey('p_sev', 'w_1')
    render(<StartDefault ws="w_1" person="p_sev" />)
    const box = screen.getByRole('switch', { name: 'Start an agent when I create a ticket' })
    expect(box).toBeChecked()
    await userEvent.click(box)
    expect(box).not.toBeChecked()
    expect(readPref(key).start).toBe(false) // the form's main button now reads "Create ticket"
    await userEvent.click(box)
    expect(readPref(key).start).toBe(true)
  })
})
