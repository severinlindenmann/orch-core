// Wiki on a real host: the empty state offers "Create the wiki folder" (owners and maintainers), which posts once and
// reloads. The Quick tasks page says it is off when the host serves quick tasks but the add-on is off.
import { cleanup, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setFeatures } from '@/api/features'
import { renderApp } from '@/test/renderApp'

const calls: { method: string; path: string }[] = []
let folder = false
vi.mock('@/api/runs', async (orig) => ({
  ...(await orig<typeof import('@/api/runs')>()),
  call: async (method: string, path: string) => {
    calls.push({ method, path })
    if (method === 'POST') {
      folder = true
      return { created: true, path: 'wiki/readme.md' }
    }
    if (path.endsWith('/wiki'))
      return folder
        ? { available: true, state: 'ok', message: null, total: 1, cut: false, pages: [{ slug: 'readme', title: 'Wiki', updated: null, by: null, tickets: [], links: 0, too_large: false }] }
        : { available: true, state: 'no_folder', message: 'No wiki folder in this project yet.', total: 0, cut: false, pages: [] }
    throw new Error('unexpected ' + path)
  },
}))

const T = { timeout: 4000 }
beforeEach(() => {
  calls.length = 0
  folder = false
})
afterEach(() => {
  cleanup()
  setFeatures(null)
})

describe('Wiki page (real host)', () => {
  it('offers to create the wiki folder, creates it once and then lists the page', async () => {
    setFeatures({ features: ['today', 'tickets', 'board', 'ticket', 'wiki'] })
    const { user } = renderApp('/wiki', { viewer: 'p_sev' })
    expect(await screen.findByText('No wiki folder in this project yet.', {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create the wiki folder' }))
    expect(await screen.findByRole('button', { name: 'Wiki' }, T)).toBeInTheDocument()
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: 'Create the wiki folder' })).not.toBeInTheDocument()
  })
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
