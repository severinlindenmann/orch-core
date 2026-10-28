import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setFeatures } from '@/api/features'

const T = { timeout: 5000 }
const W = 'a'.repeat(32)
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  setFeatures(null)
})

describe('an add-on approval in a full-mode workspace (A1: local signing in every mode)', () => {
  it('the review does not route to the hub by the workspace mode', async () => {
    const hub = vi.spyOn(api, 'hubSign')
    vi.spyOn(api, 'getWorkspaces').mockResolvedValue([{ id: W, signing: 'full' }] as unknown as Awaited<ReturnType<typeof api.getWorkspaces>>)
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { render } = await import('@testing-library/react')
    const { SignPopover } = await import('./SignPopover')
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SignPopover ws={W} title="Turn on wiki" action={{ action: 'addon_grant', name: 'wiki' } as never} onClose={() => {}} /></QueryClientProvider>)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(within(dialog).queryByTestId('hub-addon')).toBeNull()
    expect(within(dialog).queryByTestId('hub-link')).toBeNull()
    expect(hub).not.toHaveBeenCalled()
  })
})
