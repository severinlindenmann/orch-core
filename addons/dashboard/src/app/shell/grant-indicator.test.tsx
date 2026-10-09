import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const sev = { kind: 'person', id: 'p_sev', device: 'd_mac' } as const

describe('sidebar grant indicator', () => {
  it("shows the viewer's own active grant in the current workspace (Mara: until 17:00)", async () => {
    renderApp('/', { viewer: 'p_mara' })
    expect(await screen.findByText('agents granted until 17:00')).toBeInTheDocument()
  })

  it('says no grant once the grant is revoked', async () => {
    renderApp('/', { setup: (s) => void s.revokeGrant(s.workspaces[0].id, 'gr_01J9Z8', sev) })
    await screen.findByRole('heading', { name: 'Today' })
    expect(await screen.findByText('no grant · run orch grant')).toBeInTheDocument()
  })

  it('shows a re-issued grant', async () => {
    renderApp('/', {
      setup: (s) => {
        const ws = s.workspaces[0].id
        s.revokeGrant(ws, 'gr_01J9Z8', sev)
        s.issueGrant(ws, { hours: 2, scope: 'all' }, sev)
      },
    })
    expect(await screen.findByText(/^agents granted until 13:3\d$/)).toBeInTheDocument()
  })

  it('follows the current workspace (no grant for Severin in Client VM)', async () => {
    const { user } = renderApp('/')
    expect(await screen.findByText('agents granted until 18:00')).toBeInTheDocument()
    await user.keyboard('{Meta>}3{/Meta}')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Client VM/))
    expect(await screen.findByText('no grant · run orch grant')).toBeInTheDocument()
  })
})
