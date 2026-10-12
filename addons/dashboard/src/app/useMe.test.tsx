import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, mockStore, resetMockStoreForTests } from '@/api/client'
import { useMe } from './useMe'
import { useRole } from './useRole'
import { WorkspaceProvider, useWorkspace } from './workspace'

// One person has a different id in each workspace (the real host): DEMO knows the viewer as p_tom (a viewer there),
// INT as p_mara (a maintainer there). The old single `me` gave p_tom everywhere, so INT drew no role at all.
const DEMO = '6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11'
const INT = '0a5e7c31-2d9f-4b68-8e1a-5c3f7d9b2e44'
const identity: Record<string, { person: string; name: string; role: 'viewer' | 'maintainer' }> = {
  [DEMO]: { person: 'p_tom', name: 'Tom', role: 'viewer' },
  [INT]: { person: 'p_mara', name: 'Mara', role: 'maintainer' },
}

function Probe() {
  const { workspaces } = useWorkspace()
  return (
    <ul>
      {workspaces.map((w) => (
        <Row key={w.id} id={w.id} prefix={w.prefix} />
      ))}
    </ul>
  )
}
function Row({ id, prefix }: { id: string; prefix: string }) {
  const role = useRole(id)
  const me = useMe(id)
  return (
    <li>
      {prefix}: {me.data?.person ?? '-'} / {role ?? 'none'}
    </li>
  )
}

afterEach(() => vi.restoreAllMocks())

describe('identity per workspace', () => {
  it('each workspace has its own person and role', async () => {
    resetMockStoreForTests()
    mockStore.setViewer('p_sev')
    vi.spyOn(api, 'getMe').mockImplementation(async (ws?: string) => identity[ws ?? DEMO])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const root = createRootRoute({
      component: () => (
        <QueryClientProvider client={client}>
          <WorkspaceProvider>
            <Probe />
          </WorkspaceProvider>
        </QueryClientProvider>
      ),
    })
    render(<RouterProvider router={createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })} />)
    await waitFor(() => expect(screen.getByText('INT: p_mara / maintainer')).toBeInTheDocument())
    expect(screen.getByText('DEMO: p_tom / viewer')).toBeInTheDocument()
    expect(api.getMe).toHaveBeenCalledWith(INT)
    expect(api.getMe).toHaveBeenCalledWith(DEMO)
  })

  it('the mock keeps answering with its one valid Me for any workspace', async () => {
    resetMockStoreForTests()
    mockStore.setViewer('p_sev')
    expect((await api.getMe(INT)).person).toBe('p_sev')
    expect((await api.getMe()).person).toBe('p_sev')
  })
})

describe('settings pages', () => {
  it('useViewerRole reads the identity of the workspace it is given', async () => {
    const { useViewerRole } = await import('./pages/settings/host/useViewerRole')
    resetMockStoreForTests()
    mockStore.setViewer('p_sev')
    vi.spyOn(api, 'getMe').mockImplementation(async (ws?: string) => identity[ws ?? DEMO])
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    function Role({ id }: { id: string }) {
      const { workspaces } = useWorkspace()
      const w = workspaces.find((x) => x.id === id)
      const role = useViewerRole(w ?? ({ id, members: [] } as never))
      return <p>{id === INT ? 'INT' : 'DEMO'} role {role ?? 'none'}</p>
    }
    const root = createRootRoute({ component: () => (<QueryClientProvider client={client}><WorkspaceProvider><Role id={INT} /><Role id={DEMO} /></WorkspaceProvider></QueryClientProvider>) })
    render(<RouterProvider router={createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })} />)
    await waitFor(() => expect(screen.getByText('INT role maintainer')).toBeInTheDocument())
    expect(screen.getByText('DEMO role viewer')).toBeInTheDocument()
  })
})
