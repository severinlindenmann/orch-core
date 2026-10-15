import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryHistory, createRootRoute, createRouter, RouterProvider } from '@tanstack/react-router'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import { useSwitchGuard, useWorkspace, WorkspaceProvider } from './workspace'

function Probe({ a, b }: { a: ((p: () => void) => void) | null; b: ((p: () => void) => void) | null }) {
  const { workspace, workspaces, switchWorkspace } = useWorkspace()
  useSwitchGuard(a)
  useSwitchGuard(b)
  const other = workspaces.find((w) => w.id !== workspace?.id)
  return (
    <>
      <p>now: {workspace?.id}</p>
      <button type="button" disabled={!other} onClick={() => other && switchWorkspace(other.id)}>
        switch
      </button>
    </>
  )
}

function setup(a: ((p: () => void) => void) | null, b: ((p: () => void) => void) | null) {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  try {
    localStorage.clear()
  } catch {
    /* jsdom */
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRootRoute({
    component: () => (
      <QueryClientProvider client={client}>
        <WorkspaceProvider>
          <Probe a={a} b={b} />
        </WorkspaceProvider>
      </QueryClientProvider>
    ),
  })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(<RouterProvider router={router} />)
}

describe('useSwitchGuard with more than one guard', () => {
  it('asks every guard in turn and switches only when all let it through', async () => {
    const user = userEvent.setup()
    const a = vi.fn((proceed: () => void) => proceed())
    const b = vi.fn((proceed: () => void) => proceed())
    setup(a, b)
    const btn = await screen.findByRole('button', { name: 'switch' })
    await waitFor(() => expect(btn).toBeEnabled())
    const before = screen.getByText(/^now:/).textContent
    await user.click(btn)
    await waitFor(() => expect(screen.getByText(/^now:/).textContent).not.toBe(before))
    expect(a).toHaveBeenCalledTimes(1)
    expect(b).toHaveBeenCalledTimes(1)
  })
  it('stays put when the second guard says no', async () => {
    const user = userEvent.setup()
    const a = vi.fn((proceed: () => void) => proceed())
    const b = vi.fn() // asks and the person keeps editing: never proceeds
    setup(a, b)
    const btn = await screen.findByRole('button', { name: 'switch' })
    await waitFor(() => expect(btn).toBeEnabled())
    const before = screen.getByText(/^now:/).textContent
    await user.click(btn)
    await waitFor(() => expect(b).toHaveBeenCalled())
    expect(a).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/^now:/).textContent).toBe(before)
  })
})
