import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import { parseSecretsFile } from '@/api/secrets'
import { SpawnConfirm } from '@/addon-ui/SpawnConfirm'
import { replay } from '@/app/terminal/fakePty'
import { WorkspaceProvider } from '@/app/workspace'
import connectionsFixture from '@/mocks/fixtures/connections.json'
import { renderApp } from '@/test/renderApp'
import { findRail, openTicketPanel } from '@/test/ticketPanels'

const T = { timeout: 5000 }
const DEMO = '6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11'
const VALUES = parseSecretsFile(connectionsFixture.DEMO.secrets.text).entries.map((e) => e.value)

afterEach(() => vi.unstubAllGlobals())

describe('Settings › Skills', () => {
  it('lists skills by scope with sidecar status; Org is a P6 preview', async () => {
    renderApp('/settings/skills')
    const workspace = await screen.findByRole('table', { name: 'Workspace skills' }, T)
    expect(within(workspace).getByRole('row', { name: /meter-notes/ })).toHaveTextContent('unknown needs')
    expect(within(workspace).getByRole('row', { name: /dbt-seeds/ })).toHaveTextContent('orch.skill.json')
    expect(within(workspace).getByRole('row', { name: /tariff-feed/ })).toHaveTextContent('TARIFF_WEBHOOK_SECRET· needs grant')
    expect(screen.getByRole('table', { name: 'Built in skills' })).toHaveTextContent('github-prs')
    expect(screen.getByText('P6 · Preview')).toBeInTheDocument()
    expect(screen.getByText(/Org skills come with the relay in P6/)).toBeInTheDocument()
  })

  it('the detail drawer renders SKILL.md and the sidecar JSON', async () => {
    const { user } = renderApp('/settings/skills')
    await user.click(await screen.findByRole('button', { name: 'Open skill dbt-seeds' }, T))
    const drawer = await screen.findByRole('dialog', { name: /dbt-seeds/ }, T)
    expect(await within(drawer).findByRole('heading', { name: 'dbt seeds' }, T)).toBeInTheDocument() // markdown, frontmatter stripped
    expect(within(drawer).getByText(/"skill_version": "1.2.0"/)).toBeInTheDocument()
    expect(within(drawer).getByText(/Grant history · 2/)).toBeInTheDocument()
  })

  it('a credential grant is signed in core\'s dialog with its own label, then recorded', async () => {
    const { user } = renderApp('/settings/skills')
    await user.click(await screen.findByRole('button', { name: 'Open skill tariff-feed' }, T))
    const drawer = await screen.findByRole('dialog', { name: /tariff-feed/ }, T)
    await user.click(within(drawer).getByRole('button', { name: 'Grant TARIFF_WEBHOOK_SECRET…' }))
    const sign = await screen.findByRole('dialog', { name: 'Grant credentials to tariff-feed' }, T)
    expect(sign).toHaveTextContent('Env TARIFF_WEBHOOK_SECRET (not in the secrets file yet)')
    expect(sign).toHaveTextContent("Edits to the skill's prose need no signature")
    await user.click(within(sign).getByRole('button', { name: 'Sign grant' }))
    await vi.waitFor(() => expect(mockStore.wsEventsOf(DEMO).at(-1)).toMatchObject({ type: 'skill.credentials_granted', skill: 'tariff-feed', env: ['TARIFF_WEBHOOK_SECRET'], presence: 'touchid' }), T)
    expect(await within(await screen.findByRole('dialog', { name: /tariff-feed/ })).findByText(/Grant history · 2/, {}, T)).toBeInTheDocument()
  })

  it('Mara reads skills but cannot grant: the reason is shown', async () => {
    const { user } = renderApp('/settings/skills', { viewer: 'p_mara' })
    await user.click(await screen.findByRole('button', { name: 'Open skill tariff-feed' }, T))
    const drawer = await screen.findByRole('dialog', { name: /tariff-feed/ }, T)
    expect(within(drawer).getByTestId('grant-reason')).toHaveTextContent('Only owners grant credentials to skills.')
    expect(within(drawer).queryByRole('button', { name: /Grant/ })).toBeNull()
  })
})

describe('Settings › Connections', () => {
  it('shows each check state, with expected vs actual for the wrong identity', async () => {
    renderApp('/settings/connections')
    const status = (name: string) => within(screen.getByTestId(`connection-${name}`)).getByTestId('check-status')
    await screen.findByTestId('connection-gh', {}, T)
    expect(status('gh')).toHaveTextContent('ok')
    expect(status('databricks-prod')).toHaveTextContent('auth expired')
    expect(status('gcloud-billing')).toHaveTextContent('wrong identity')
    expect(status('tariff-api')).toHaveTextContent('service down')
    expect(status('az-storage')).toHaveTextContent('unknown')
    expect(screen.getByTestId('connection-gcloud-billing')).toHaveTextContent('expected orch-agent@acme-energy.iam.gserviceaccount.com, got severin@acme-energy.ch')
  })

  it('Run check shows the bounded, filtered output in Details; a CLI login says orch never stores its token', async () => {
    const { user } = renderApp('/settings/connections')
    await user.click(await screen.findByRole('button', { name: 'Run check databricks-ci' }, T))
    await user.click(screen.getByRole('button', { name: 'Details databricks-ci' }))
    const out = await screen.findByLabelText('Check output databricks-ci', {}, T)
    expect(out).toHaveTextContent('Authorization: Bearer •••• (DATABRICKS_TOKEN)')
    await user.click(screen.getByRole('button', { name: 'Details databricks-prod' }))
    expect(screen.getByTestId('details-databricks-prod')).toHaveTextContent("orch never copies or stores this tool's token")
    expect(screen.getByTestId('details-databricks-prod')).toHaveTextContent('databricks auth login --profile prod')
  })

  it('the secrets file shows path, permissions and names, never a value', async () => {
    renderApp('/settings/connections')
    const panel = await screen.findByTestId('secrets-file', {}, T)
    expect(panel).toHaveTextContent(`/secrets/${DEMO}.env`)
    expect(panel).toHaveTextContent('directory 0700, file 0600, owner orch-agent')
    expect(panel).toHaveTextContent('DATABRICKS_TOKEN')
    expect(panel).toHaveTextContent('Line 8 ignored: "export" is not allowed')
    expect(panel).toHaveTextContent(/s_77c2.*DEMO-0043/)
    for (const v of VALUES) expect(document.body.textContent).not.toContain(v)
  })

  it('Run doctor lists every check and the skill with unknown needs', async () => {
    const { user } = renderApp('/settings/connections')
    await user.click(await screen.findByRole('button', { name: 'Run doctor' }, T))
    const report = await screen.findByTestId('doctor-report', {}, T)
    expect(report).toHaveTextContent('6 checks · 4 not ok · 1 skill with unknown needs')
    expect(report).toHaveTextContent('meter-notes')
    expect(report).toHaveTextContent('tariff-feed: TARIFF_WEBHOOK_SECRET')
  })

  it('Tom cannot run checks and does not see the secrets file', async () => {
    renderApp('/settings/connections', { viewer: 'p_tom' })
    expect(await screen.findByRole('button', { name: 'Run check gh' }, T)).toBeDisabled()
    expect(screen.getByText('Only owners and maintainers see the secrets file (names only).')).toBeInTheDocument()
  })
})

describe('a ticket blocked by a connection', () => {
  it('shows the block and what the ticket needs, and Start agent is refused with that reason', async () => {
    vi.stubGlobal('innerWidth', 1440)
    const { user } = renderApp('/ticket/DEMO-0053')
    expect(await screen.findByTestId('connection-blocked', {}, T)).toHaveTextContent('Blocked: databricks-prod auth expired')
    const rail = await findRail()
    expect(within(rail).getByTestId('ticket-needs')).toHaveTextContent('skills orch, databricks-sql')
    expect(within(rail).getByTestId('ticket-needs')).toHaveTextContent('connections databricks-prod (auth expired)')
    const panel = await openTicketPanel(user, 'Start agent')
    const start = await within(panel).findByRole('button', { name: 'Start' }, T)
    expect(start).toBeDisabled()
    expect(within(panel).getByRole('alert')).toHaveTextContent('Blocked: databricks-prod auth expired.')
    await user.click(screen.getByRole('button', { name: /Actions/ }))
    const item = await screen.findByRole('menuitem', { name: /Start agent/ })
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(item).toHaveTextContent('Blocked: databricks-prod auth expired')
  })

  it('SpawnConfirm refuses before showing facts', async () => {
    resetMockStoreForTests()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <WorkspaceProvider>
          <SpawnConfirm addon="start-agent" ticketKey="DEMO-0052" onStart={() => {}} onClose={() => {}} />
        </WorkspaceProvider>
      </QueryClientProvider>,
    )
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText(/Blocked: gcloud-billing wrong identity\./, {}, T)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Start agent' })).toBeNull()
  })
})

describe('Today: re-login', () => {
  it('the owner gets one item per failing login with the hint; Run check again clears it after the login', async () => {
    const { user } = renderApp('/')
    const row = await screen.findByTestId('relogin-databricks-prod', {}, T)
    expect(screen.getByTestId('relogin-gcloud-billing')).toHaveTextContent('wrong identity: expected orch-agent@acme-energy.iam.gserviceaccount.com, got severin@acme-energy.ch')
    await user.click(within(row).getByRole('button', { name: 'Re-login needed: databricks-prod' }))
    expect(within(row).getByText(/^databricks auth login --profile prod/)).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Copy the login command for databricks-prod' })).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Run check again' }))
    await vi.waitFor(() => expect(screen.queryByTestId('relogin-databricks-prod')).toBeNull(), T)
    expect(mockStore.ticket('DEMO-0053')!.needs?.blocked).toBeNull()
  })

  it('Mara gets no re-login item', async () => {
    renderApp('/', { viewer: 'p_mara' })
    await screen.findByRole('heading', { name: 'Today' }, T)
    await screen.findByText(/need you/, {}, T)
    expect(screen.queryByTestId('relogin-databricks-prod')).toBeNull()
  })
})

describe('output filtering in a terminal transcript', () => {
  it('a secret printed by a tool reaches the terminal as •••• (NAME)', () => {
    const ctx = { user: 'claude', cwd: '~/energy', branch: 'b', owner: 'agent' as const, now: '2026-10-09T11:30:00Z', cursor: 1, grant: null, claim: null, ticket: null, secrets: ['DATABRICKS_HOST', 'DATABRICKS_TOKEN'] }
    const out = replay(ctx, ['databricks current-user me --debug'])
    expect(out).toContain('> * Authorization: Bearer •••• (DATABRICKS_TOKEN)')
    expect(out).toContain('> * Host: •••• (DATABRICKS_HOST)')
    expect(out).not.toMatch(/sim-databricks_token/)
    // Without the env names (another ticket's session), the tool has no token at all.
    expect(replay({ ...ctx, secrets: [] }, ['databricks current-user me'])).toContain('no DATABRICKS_TOKEN for this session')
  })

  it("the DEMO-0043 agent's mirror transcript includes the masked line", () => {
    resetMockStoreForTests()
    const state = mockStore.addonStateView(DEMO, 'terminals') as { sessions: { id: string; ctx: Parameters<typeof replay>[0]; transcript: string[] }[] }
    const agent = state.sessions.find((s) => s.id === 'agent1')!
    expect(agent.ctx.secrets).toEqual(['DATABRICKS_HOST', 'DATABRICKS_TOKEN'])
    expect(replay(agent.ctx, agent.transcript)).toContain('•••• (DATABRICKS_TOKEN)')
  })
})
