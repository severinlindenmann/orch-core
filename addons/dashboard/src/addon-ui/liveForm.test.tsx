import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import { WorkspaceProvider } from '@/app/workspace'
import { installAndGrant } from '@/test/installAddon'
import { AddonNode } from './AddonNode'

const live = (addon: string, action: string) => {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  installAndGrant(mockStore, mockStore.workspaces.find((w) => w.prefix === 'DEMO')!.id, 'activity')
  const node = { type: 'form', schema: { type: 'object', properties: { q: { type: 'string', title: 'Search' } } }, action, live: true, submitLabel: 'Apply' }
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <WorkspaceProvider>
        <AddonNode node={node} addon={addon} />
      </WorkspaceProvider>
    </QueryClientProvider>,
  )
}

// `form.live` (apply on change) is honoured only for a navigation action without a core dialog.
describe('live forms', () => {
  it('a navigation action applies on change: no submit button', async () => {
    live('activity', 'apply')
    await screen.findByRole('textbox', { name: 'Search' })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull())
  })
  it('any other action keeps its submit button (a change never posts it)', async () => {
    live('estimate', 'set')
    await screen.findByRole('textbox', { name: 'Search' })
    expect(await screen.findByRole('button', { name: 'Apply' })).toBeInTheDocument()
  })
})
