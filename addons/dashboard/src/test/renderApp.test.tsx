import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { renderApp } from './renderApp'
import { api } from '@/api/client'

describe('renderApp harness', () => {
  it('answers without simulated latency', async () => {
    const t0 = performance.now()
    await api.getMe()
    expect(performance.now() - t0).toBeLessThan(50)
  })
  it('starts every render from the seed and the chosen viewer', async () => {
    await api.setViewer('p_tom')
    renderApp('/', { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
    expect((await api.getMe()).person).toBe('p_sev')
  })
})
