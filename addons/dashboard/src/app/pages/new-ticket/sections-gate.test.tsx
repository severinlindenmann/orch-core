import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { neededBefore } from '@/api/sections'
import { renderApp } from '@/test/renderApp'

describe('which approval needs a section (N3)', () => {
  it('Context and Out of scope are needed before the REQUIREMENTS gate, Plan and Decisions before the plan gate', () => {
    expect(neededBefore('feature', 'context')).toBe('requirements')
    expect(neededBefore('feature', 'out_of_scope')).toBe('requirements')
    expect(neededBefore('feature', 'plan')).toBe('plan')
    expect(neededBefore('feature', 'decisions')).toBe('plan')
    expect(neededBefore('chore', 'context')).toBeNull() // a chore's requirements gate asks for the requirements only
    expect(neededBefore('chore', 'out_of_scope')).toBeNull()
    expect(neededBefore('spike', 'context')).toBe('requirements')
    expect(neededBefore('feature', 'verification')).toBeNull() // no gate asks for it before the verdict
  })
  it('the form says so', async () => {
    const { user } = renderApp('/tickets/new')
    await screen.findByLabelText('Title')
    await user.click(screen.getByRole('button', { name: /More options/ })) // the rest of the sections sit in the fold
    expect(screen.getAllByText('needed before the requirements gate')).toHaveLength(2) // context, out of scope
    expect(screen.getAllByText('needed before the plan gate')).toHaveLength(2) // plan, decisions
    expect(screen.queryByText(/plan gate/, { selector: '#nothing' })).toBeNull()
  })
})
