import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import { WorkspaceProvider } from '@/app/workspace'
import { AddonNode } from './AddonNode'

const form = (label: string, action: string) => ({ type: 'form', schema: { type: 'object', properties: { a: { type: 'string', title: label } } }, action, submitLabel: `Save ${label}` })

function setup(node: unknown, onState: (s: { pending: boolean; blocked: boolean }) => void) {
  resetMockStoreForTests()
  mockStore.setViewer('p_sev')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const at = (n: unknown) => (
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <AddonNode node={n} addon="estimate" formControl={{ id: 'drawer-form', onState }} />
      </WorkspaceProvider>
    </QueryClientProvider>
  )
  const view = render(at(node))
  return { ...view, show: (n: unknown) => view.rerender(at(n)) }
}

describe('the drawer form control belongs to one form', () => {
  it('the first form takes it; the second keeps its own submit button', async () => {
    setup({ type: 'stack', children: [form('One', 'one'), form('Two', 'two')] }, () => {})
    await waitFor(() => expect(document.querySelectorAll('form#drawer-form')).toHaveLength(1))
    expect(screen.queryByRole('button', { name: 'Save One' })).toBeNull()
    expect(await screen.findByRole('button', { name: 'Save Two' })).toBeInTheDocument()
  })
  it('a form that lost never reports its state to the drawer', async () => {
    const onState = vi.fn()
    setup({ type: 'stack', children: [form('One', 'one'), form('Two', 'two')] }, onState)
    await waitFor(() => expect(document.querySelectorAll('form#drawer-form')).toHaveLength(1))
    expect(onState).toHaveBeenCalledTimes(1) // only the winner's report; the loser's would be a second call
  })
  it('when the winner goes away, the waiting form takes the control', async () => {
    const view = setup({ type: 'stack', children: [form('One', 'one'), form('Two', 'two')] }, () => {})
    await screen.findByRole('button', { name: 'Save Two' })
    view.show({ type: 'stack', children: [{ type: 'markdown', text: 'gone' }, form('Two', 'two')] })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save Two' })).toBeNull())
    expect(document.querySelectorAll('form#drawer-form')).toHaveLength(1)
  })
})
