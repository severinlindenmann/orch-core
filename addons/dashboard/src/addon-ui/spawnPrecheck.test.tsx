import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import { WorkspaceProvider } from '@/app/workspace'
import { AddonNode } from './AddonNode'
import { SpawnConfirm } from './SpawnConfirm'

const T = { timeout: 5000 }
const REASON = 'DEMO-0037 is claimed by Codex for Mara. Stop that session on Agents first.'

function wrap(ui: ReactNode) {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceProvider>{ui}</WorkspaceProvider>
    </QueryClientProvider>,
  )
}

describe("core's start-agent precheck on a claimed ticket", () => {
  it('SpawnConfirm refuses before showing any facts: the reason, and no Start agent button', async () => {
    wrap(<SpawnConfirm addon="start-agent" ticketKey="DEMO-0037" onStart={() => {}} onClose={() => {}} />)
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText(REASON, {}, T)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Start agent' })).toBeNull()
    expect(within(dialog).queryByLabelText('What orch will start')).toBeNull()
  })

  it('a form whose action starts an agent is disabled with the reason, like a button', async () => {
    const ticket = mockStore.ticket('DEMO-0037')!
    const form = { type: 'form', schema: { type: 'object', properties: { mode: { type: 'string', title: 'Mode' } } }, action: 'start', submitLabel: 'Start now' }
    wrap(<AddonNode node={form} addon="start-agent" ctx={{ ticket }} />)
    expect(await screen.findByRole('alert', {}, T)).toHaveTextContent(REASON)
    expect(await screen.findByRole('button', { name: 'Start now' }, T)).toBeDisabled()
  })
})
