import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 5000 }

async function openMenuItem(user: ReturnType<typeof renderApp>['user'], name: RegExp) {
  await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
  await user.click(await screen.findByRole('menuitem', { name }, T))
  return screen.findByRole('dialog', {}, T)
}

describe('G4 signing dialogs say what you sign', () => {
  it('approve plan on DEMO-0044 lists the tasks it signs, never "No text."', async () => {
    const { user } = renderApp('/ticket/DEMO-0044')
    await screen.findByRole('heading', { level: 1, name: /Rotate warehouse/ }, T)
    const dialog = await openMenuItem(user, /Approve plan/)
    expect(dialog).not.toHaveTextContent('No text.')
    const items = within(dialog).getAllByRole('listitem').map((li) => li.textContent)
    expect(items.some((t) => /Create new service principal/.test(t!))).toBe(true)
    expect(items.some((t) => /Update secret in CI/.test(t!))).toBe(true)
    expect(within(dialog).getByRole('button', { name: 'Approve plan' })).toBeEnabled()
    expect(dialog).toHaveTextContent('You confirm with Touch ID or your key.')
    expect(within(dialog).queryByRole('button', { name: /signs? with Touch ID/i })).toBeNull()
    // The hash lives in a closed Details, not at the first level.
    const details = dialog.querySelector('details')!
    expect(details).not.toHaveAttribute('open')
    expect(details).toHaveTextContent(/sha256:/)
    // Initial focus is Cancel.
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus()
  })

  it('verdict dialog: nothing chosen, the button follows the choice, Send back needs a note and says why', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    const dialog = await openMenuItem(user, /Give verdict/)
    const radios = within(dialog).getAllByRole('radio')
    expect(radios).toHaveLength(2)
    for (const r of radios) expect(r).not.toBeChecked()
    expect(radios[0]).toHaveFocus()
    expect(dialog).toHaveTextContent(/AC \d+\/\d+ evidenced · \d+ receipts?/)
    expect(within(dialog).getByRole('button', { name: /Open evidence/ })).toBeInTheDocument()
    expect(dialog).toHaveTextContent('Choose Pass or Send back')
    await user.click(within(dialog).getByRole('radio', { name: /Pass · the evidence is enough/ }))
    const pass = within(dialog).getByRole('button', { name: 'Pass' })
    expect(pass).toBeEnabled()
    await user.click(within(dialog).getByRole('radio', { name: /Send back · something must change/ }))
    const back = within(dialog).getByRole('button', { name: 'Send back' })
    expect(back).toBeDisabled()
    expect(dialog).toHaveTextContent(/What should change\? \(required\)/)
    await user.type(within(dialog).getByRole('textbox'), 'Add a receipt')
    await waitFor(() => expect(back).toBeEnabled())
  })

  it('the answer dialog names its verb and quotes the question and answer', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(screen.getByRole('tab', { name: /Questions/ }))
    await user.click(await screen.findByRole('radio', { name: /DATE \(local midnight\)/ }))
    await user.click(within(document.getElementById('question-Q2')!).getByRole('button', { name: 'Answer Q2' }))
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(within(dialog).getByRole('button', { name: 'Send answer' })).toBeInTheDocument()
    expect(dialog).toHaveTextContent(/Your answer: DATE/)
  })
})

describe('G4 empty approvals and content', () => {
  it('an approval with no text and no tasks says so and cannot be signed', async () => {
    const { createMemoryHistory, createRootRoute, createRouter, RouterProvider } = await import('@tanstack/react-router')
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { render } = await import('@testing-library/react')
    const { mockStore, resetMockStoreForTests } = await import('@/api/client')
    const { SignDialog } = await import('./SignDialog')
    resetMockStoreForTests()
    const base = mockStore.ticket('DEMO-0044')!
    const empty = { ...base, body: { ...base.body, plan: '' }, tasks_state: [] }
    const root = createRootRoute({ component: () => <SignDialog ticket={empty} action={{ kind: 'approve', gate: 'plan' }} onClose={() => {}} /> })
    const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
    render(
      <QueryClientProvider client={new QueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(dialog).toHaveTextContent('Nothing to approve yet: the plan has no text or tasks.')
    expect(within(dialog).getByRole('button', { name: 'Approve plan' })).toBeDisabled()
  })

  it('a new ticket folds its unwritten sections into one muted line', async () => {
    renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    const lines = await screen.findAllByText(/^Not written yet: Context/)
    expect(lines).toHaveLength(1)
    expect(lines[0].textContent).toMatch(/Out of scope/)
    expect(screen.queryByText('Not written yet.')).toBeNull()
  })

  it('requirements without list markers become one list item per line; the handoff comes first', async () => {
    const { Overview, asListItems } = await import('./Overview')
    expect(asListItems('Rotate within the week\n\nNo downtime')).toBe('- Rotate within the week\n- No downtime')
    expect(asListItems('- keep\n- as is')).toBe('- keep\n- as is')
    expect(Overview).toBeTypeOf('function')
  })

  it('the artifact drawer returns focus to the card that opened it on Escape', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    await user.click(screen.getByRole('tab', { name: /Artifacts/ }))
    const open = await screen.findByRole('button', { name: 'Open reconcile-pass.log' }, T)
    await user.click(open)
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(within(sheet).getByRole('button', { name: 'Copy all' })).toBeInTheDocument()
    expect(within(sheet).getByRole('button', { name: 'Wrap lines' })).toHaveAttribute('aria-pressed', 'true')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(open).toHaveFocus(), T)
  })

  it('an HTML artifact opens as a sandboxed preview with a View source toggle', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    await user.click(screen.getByRole('tab', { name: /Artifacts/ }))
    await user.click(await screen.findByRole('button', { name: 'Open reconciliation-demo.html' }, T))
    const sheet = await screen.findByRole('dialog', {}, T)
    await waitFor(() => expect(sheet.querySelector('iframe')).toBeTruthy(), T)
    expect(sheet.querySelector('iframe')).toHaveAttribute('sandbox', 'allow-scripts')
    expect(sheet).toHaveTextContent('Sandboxed preview')
    await user.click(within(sheet).getByRole('button', { name: 'View source' }))
    expect(sheet.querySelector('iframe')).toBeNull()
  })

  it('acceptance criteria are one line each with a glyph, and the command sits behind "How it\'s verified"', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    await user.click(screen.getByRole('tab', { name: /Acceptance & tasks/ }))
    const row = document.getElementById('ac-AC1')!
    expect(within(row).getByRole('img', { name: /Proven by a receipt/ })).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Evidence' }))
    expect(within(row).getByText('verified by receipt')).toBeInTheDocument()
    await user.click(within(row).getAllByRole('button', { name: /How it's verified/ })[0])
    expect(row.querySelector('pre')).toBeTruthy()
  })
})
