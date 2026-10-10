import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 5000 }

// The primary action sits in the header (G3); everything else is in the Actions menu.
async function openMenuItem(user: ReturnType<typeof renderApp>['user'], name: RegExp) {
  const header = await screen.findByTestId('ticket-header', {}, T)
  const primary = within(header).queryByRole('button', { name })
  if (primary) {
    await user.click(primary)
  } else {
    await user.click(await screen.findByRole('button', { name: /Actions/ }, T))
    await user.click(await screen.findByRole('menuitem', { name }, T))
  }
  return screen.findByRole('dialog', {}, T)
}

describe('G4 signing dialogs say what you sign', () => {
  it('approve plan on DEMO-0044 lists the tasks it signs, never "No text."', async () => {
    const { user } = renderApp('/ticket/DEMO-0044')
    await screen.findByRole('heading', { level: 1, name: /Rotate warehouse/ }, T)
    const dialog = await openMenuItem(user, /Approve plan/)
    expect(dialog).not.toHaveTextContent('No text.')
    expect(within(dialog).getByRole('region', { name: 'Tasks' })).toHaveTextContent(/T1\s+Create new service principal/)
    expect(within(dialog).getByRole('region', { name: 'Tasks' })).toHaveTextContent(/T2\s+Update secret in CI/)
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
    // The verdict signs the commit: the head of the branch, with its diffstat, in the choice and in the covers.
    expect(within(dialog).getByRole('button', { name: 'Open changes' })).toBeInTheDocument()
    expect(within(dialog).getByRole('region', { name: 'Commit' })).toHaveTextContent(/Commit c90e7a1 on feat\/DEMO-0041-reconciliation: \+\d+ \u2212\d+ in \d+ files? against develop/)
    await user.click(within(dialog).getByRole('radio', { name: /^Pass on c90e7a1 · \+\d+ \u2212\d+: the evidence is enough$/ }))
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
    const empty = { ...base, body: { ...base.body, plan: '' }, tasks: [], tasks_state: [] }
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

async function renderDialog(ticketPatch: (t: ReturnType<typeof baseTicket>) => unknown, action: import('./shared').HumanAction) {
  const { createMemoryHistory, createRootRoute, createRouter, RouterProvider } = await import('@tanstack/react-router')
  const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
  const { render } = await import('@testing-library/react')
  const { SignDialog } = await import('./SignDialog')
  const ticket = ticketPatch(baseTicket()) as import('@/api/types').TicketDocument
  const root = createRootRoute({ component: () => <SignDialog ticket={ticket} action={action} onClose={() => {}} /> })
  const router = createRouter({ routeTree: root, history: createMemoryHistory({ initialEntries: ['/'] }) })
  render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return screen.findByRole('dialog', {}, T)
}
function baseTicket() {
  resetStore()
  return structuredClone(storeTicket('DEMO-0044')!)
}
import { mockStore as store, resetMockStoreForTests as resetStore } from '@/api/client'
const storeTicket = (k: string) => store.ticket(k)

describe('G4 the dialog shows everything the gate hash covers', () => {
  it('plan: decisions and a task verify command appear; a plan with only Decisions is not empty', async () => {
    const dialog = await renderDialog(
      (t) => ({ ...t, body: { ...t.body, plan: '', decisions: 'Use a new principal, not a key rotation.' }, tasks: [], tasks_state: [] }),
      { kind: 'approve', gate: 'plan' },
    )
    expect(dialog).toHaveTextContent('Use a new principal, not a key rotation.')
    expect(dialog).not.toHaveTextContent('Nothing to approve yet')
    expect(within(dialog).getByRole('button', { name: 'Approve plan' })).toBeEnabled()
  })
  it('plan: shows the verify command and what a task proves', async () => {
    const dialog = await renderDialog((t) => t, { kind: 'approve', gate: 'plan' })
    await waitFor(() => expect(dialog).toHaveTextContent('Assignee: Severin'), T)
    expect(dialog).toHaveTextContent('Assignee: Mara')
    expect(dialog).toHaveTextContent('Verify: gh workflow run nightly')
    expect(dialog).toHaveTextContent('Proves: AC1')
  })
  it('requirements: out of scope, acceptance criteria, type and size appear', async () => {
    const dialog = await renderDialog((t) => ({ ...t, body: { ...t.body, out_of_scope: 'No vault migration.' } }), { kind: 'approve', gate: 'requirements' })
    expect(dialog).toHaveTextContent('No vault migration.')
    expect(dialog).toHaveTextContent('Old credentials are revoked')
    expect(dialog).toHaveTextContent(/Type: chore · Size: xs/)
  })
  it('requirements with no text and no criteria say so in their own words', async () => {
    const dialog = await renderDialog((t) => ({ ...t, body: { ...t.body, requirements: '', out_of_scope: '' }, acceptance: [], acceptance_state: [] }), { kind: 'approve', gate: 'requirements' })
    expect(dialog).toHaveTextContent('Nothing to approve yet: the requirements have no text or acceptance criteria.')
    expect(within(dialog).getByRole('button', { name: 'Approve requirements' })).toBeDisabled()
  })
  it('request changes on an empty section shows no empty box', async () => {
    const dialog = await renderDialog((t) => ({ ...t, body: { ...t.body, plan: '' }, tasks: [], tasks_state: [] }), { kind: 'request_changes', gate: 'plan' })
    expect(dialog.querySelector('section')).toBeNull()
  })
  it('the hash material and the dialog come from one function', async () => {
    const { gateSignedContent } = await import('@/api/gates')
    const t = baseTicket()
    expect(t.gates.plan.covers).toEqual(gateSignedContent('plan', t).covers)
    expect(t.gates.requirements.covers).toEqual(gateSignedContent('requirements', t).covers)
  })
})

describe('G4 artifact drawer stays closed to stale state', () => {
  it('turning agent HTML off while the drawer is open replaces the frame with source', async () => {
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { render } = await import('@testing-library/react')
    const userEvent = (await import('@testing-library/user-event')).default
    const { Artifacts } = await import('./Artifacts')
    const t = baseTicket()
    const art = { name: 'demo.html', kind: 'other', preview: '<p>hi</p>', sha256: 'b'.repeat(64), bytes: 10, at: '2026-10-09T08:00:00Z', added_by: 'host' }
    const ticket = { ...t, artifacts: [art] } as unknown as import('@/api/types').TicketDocument
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <Artifacts {...({ ticket, viewer: { name: (id: string) => id }, jump: () => {}, sign: () => {} } as unknown as import('./shared').TabProps)} />
      </QueryClientProvider>,
    )
    await userEvent.setup().click(screen.getByRole('button', { name: 'Open demo.html' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    await waitFor(() => expect(sheet.querySelector('iframe')).toBeTruthy(), T)
    const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
    store.addonOp(ws, 'widgets', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
    await client.invalidateQueries()
    await waitFor(() => expect(sheet.querySelector('iframe')).toBeNull(), T)
    expect(sheet).toHaveTextContent('Agent HTML is off')
  })
  it('a .html-named log is shown as text, not run', async () => {
    const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
    const { render } = await import('@testing-library/react')
    const userEvent = (await import('@testing-library/user-event')).default
    const { Artifacts } = await import('./Artifacts')
    const t = baseTicket()
    const art = { name: 'run.html', kind: 'log', preview: '<script>alert(1)</script>', sha256: 'a'.repeat(64), bytes: 10, at: '2026-10-09T08:00:00Z', added_by: 'host' }
    const viewer = { name: (id: string) => id }
    const ticket = { ...t, artifacts: [art] } as unknown as import('@/api/types').TicketDocument
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Artifacts {...({ ticket, viewer, jump: () => {}, sign: () => {} } as unknown as import('./shared').TabProps)} />
      </QueryClientProvider>,
    )
    await userEvent.setup().click(screen.getByRole('button', { name: 'Open run.html' }))
    const sheet = await screen.findByRole('dialog', {}, T)
    expect(sheet.querySelector('iframe')).toBeNull()
    expect(within(sheet).getByRole('button', { name: 'Wrap lines' })).toBeInTheDocument()
  })
  it('switching artifacts resets wrap and source', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    await user.click(screen.getByRole('tab', { name: /Artifacts/ }))
    const log = await screen.findByRole('button', { name: 'Open reconcile-pass.log' }, T)
    await user.click(log)
    let sheet = await screen.findByRole('dialog', {}, T)
    await user.click(within(sheet).getByRole('button', { name: 'Wrap lines' }))
    expect(within(sheet).getByRole('button', { name: 'Wrap lines' })).toHaveAttribute('aria-pressed', 'false')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
    await user.click(log)
    sheet = await screen.findByRole('dialog', {}, T)
    expect(within(sheet).getByRole('button', { name: 'Wrap lines' })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('G4 every SignPrompt names its verb', () => {
  it('each <SignPrompt> element carries confirmLabel', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const walk = (d: string): string[] => fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]))
    const files = walk(path.resolve(__dirname, '../../..')).filter((f) => /\.tsx$/.test(f) && !/\.test\.|SignPrompt\.tsx/.test(f))
    const bad: string[] = []
    for (const f of files) {
      const src = fs.readFileSync(f, 'utf8')
      // Each opening tag, up to its closing `>` outside braces.
      for (const m of src.matchAll(/<SignPrompt\b/g)) {
        let i = m.index! + m[0].length
        let depth = 0
        while (i < src.length && !(src[i] === '>' && depth === 0)) {
          if (src[i] === '{') depth++
          if (src[i] === '}') depth--
          i++
        }
        if (!/\bconfirmLabel\b/.test(src.slice(m.index!, i))) bad.push(`${path.basename(f)}:${src.slice(0, m.index!).split('\n').length}`)
      }
    }
    expect(bad).toEqual([])
  })
})
