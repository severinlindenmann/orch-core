// The Quick tasks page says it is off when the host serves quick tasks but the add-on is off.
import { cleanup, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { setFeatures } from '@/api/features'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
afterEach(() => {
  cleanup()
  setFeatures(null)
})

describe('Quick tasks page when the add-on is off', () => {
  it('says it is off, offers no input and links to Add-ons', async () => {
    setFeatures({ features: ['today', 'tickets', 'board', 'ticket', 'quick'] })
    renderApp('/quick', { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { name: 'Quick tasks is off' }, T)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Add-ons' })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByTestId('quick-unavailable')).not.toBeInTheDocument())
  })
})
