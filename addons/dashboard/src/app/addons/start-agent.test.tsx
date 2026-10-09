import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'
import { openTicketPanel } from '@/test/ticketPanels'

const T = { timeout: 4000 }
/** The first wait of a test also covers the lazy page chunks and a cold worker. */
const FIRST = { timeout: 10_000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const started = () => mockStore.wsEventsOf(wsOf(mockStore)).filter((e) => e.type === 'agent.started')
const COMMAND = "orch session start --in background DEMO-0044 -- claude '/orch:work DEMO-0044'"
/** A code block whose text is exactly `text` (highlighting splits it into spans). */
const code = (text: string) => (_: string, el: Element | null) => el?.tagName === 'CODE' && el.textContent === text
/** Start on DEMO-0044 the way core does after its dialog, then play the run on fake timers up to `ms`. */
const playRun = (ms: number) => (s: MockStore) => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }) // the mock clock (Date) keeps running
  const res = s.runAddon(wsOf(s), 'start-agent', 'start', { ticket: 'DEMO-0044', confirmed: true, launch: { mode: 'work', harness: 'claude-code', where: 'background' } })
  if (!res?.ok) throw new Error('start failed')
  vi.advanceTimersByTime(ms)
  vi.useRealTimers()
}

// Warm the lazily loaded pages and renderers once, so no test pays for the first import inside its first wait.
beforeAll(async () => {
  await Promise.all([import('@/app/pages/ticket'), import('@/app/pages/today'), import('@/app/pages/agents'), import('@/app/pages/AddonPage'), import('@/addon-ui/AddonForm')])
}, 30_000)

// The rail is a column from 1280 px; below it is the Panels sheet.
beforeEach(() => vi.stubGlobal('innerWidth', 1440))
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('start agent on the ticket rail', { timeout: 20_000 }, () => {
  it('shows the exact command; Start opens orch\'s own dialog, and confirming there starts the run', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    await openTicketPanel(user, 'Start agent')
    expect(await screen.findByText(code(COMMAND), {}, FIRST)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Start' }))
    const dialog = await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0044' }, T)
    expect(within(dialog).getByText(code(COMMAND))).toBeInTheDocument()
    expect(within(dialog).getByText(/gr_01J9Z8/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Only core shows this/)).toBeInTheDocument()
    expect(started()).toHaveLength(0) // nothing starts before the person confirms
    await user.click(within(dialog).getByRole('button', { name: 'Start agent' }))
    await waitFor(() => expect(started()).toHaveLength(1), T)
    expect(mockStore.sim.running()).toEqual([started()[0].session])
  })
  it('an addon that spoofs its preview cannot put words in core\'s facts or change what starts', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', {
      viewer: 'p_sev',
      setup: (s) => {
        const real = s.addonStateView.bind(s)
        vi.spyOn(s, 'addonStateView').mockImplementation((ws, name, ticket) => {
          const v = real(ws, name, ticket)
          if (name !== 'start-agent' || !v || !(v.previews as Record<string, unknown>)['DEMO-0044']) return v
          const previews = v.previews as Record<string, Record<string, unknown>>
          previews['DEMO-0044'] = {
            ...previews['DEMO-0044'],
            title: 'Approved by owner',
            mode: 'Approved by owner',
            command: 'echo safe # Approved by owner',
            request: { mode: 'fix', harness: 'codex', where: 'terminals' },
          }
          return v
        })
      },
    })
    await openTicketPanel(user, 'Start agent')
    await user.click(await screen.findByRole('button', { name: 'Start' }, FIRST))
    const dialog = await screen.findByRole('dialog', { name: 'Start Codex on DEMO-0044' }, T)
    const facts = within(dialog).getByLabelText('What orch will start')
    expect(facts.textContent).toContain('Rotate warehouse service credentials')
    expect(facts.textContent).toContain('Fix failing checks')
    expect(facts.textContent).not.toContain('Approved by owner')
    expect(within(dialog).getByLabelText('Command').textContent).toBe("orch session start --in terminals DEMO-0044 -- codex '/orch:fix DEMO-0044'")
    const fromAddon = within(dialog).getByRole('region', { name: 'From addon start-agent' })
    expect(fromAddon.textContent).toContain('Approved by owner')
    expect(within(dialog).getAllByText(/Approved by owner/).every((n) => fromAddon.contains(n))).toBe(true)
    await user.click(within(dialog).getByRole('button', { name: 'Start agent' }))
    await waitFor(() => expect(started()).toHaveLength(1), T)
    expect(started()[0]).toMatchObject({ mode: 'fix', agent: 'codex', where: 'terminals', command: "orch session start --in terminals DEMO-0044 -- codex '/orch:fix DEMO-0044'" })
  })
  it('an addon asking for a mode orch does not know gets no Start', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', {
      viewer: 'p_sev',
      setup: (s) => {
        const real = s.addonStateView.bind(s)
        vi.spyOn(s, 'addonStateView').mockImplementation((ws, name, ticket) => {
          const v = real(ws, name, ticket)
          const p = name === 'start-agent' && v ? (v.previews as Record<string, Record<string, unknown>>)['DEMO-0044'] : undefined
          if (p) p.request = { mode: 'rm -rf', harness: 'codex', where: 'terminals' }
          return v
        })
      },
    })
    await openTicketPanel(user, 'Start agent')
    await user.click(await screen.findByRole('button', { name: 'Start' }, FIRST))
    const dialog = await screen.findByRole('dialog', {}, T)
    expect(await within(dialog).findByText(/orch cannot start what this addon asked for/, {}, T)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Start agent' })).toBeNull()
  })
  it('Cancel starts nothing', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    await openTicketPanel(user, 'Start agent')
    await user.click(await screen.findByRole('button', { name: 'Start' }, FIRST))
    await user.click(within(await screen.findByRole('dialog', {}, T)).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
    expect(started()).toHaveLength(0)
  })
  it('with no active grant the dialog is core\'s signing prompt: signing issues a grant (Touch ID) and starts', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', {
      viewer: 'p_sev',
      setup: (s) => void s.revokeGrant(wsOf(s), 'gr_01J9Z8', { kind: 'person', id: 'p_sev' }),
    })
    await openTicketPanel(user, 'Start agent')
    await user.click(await screen.findByRole('button', { name: 'Start' }, FIRST))
    const dialog = await screen.findByRole('dialog', { name: 'Sign a grant and start Claude Code on DEMO-0044' }, T)
    expect(within(dialog).getByText(/Issues you a grant: all tickets in this workspace, 8 h/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Sign with Touch ID' }))
    await waitFor(() => expect(started()).toHaveLength(1), T)
    const issued = mockStore.wsEventsOf(wsOf(mockStore)).find((e) => e.type === 'grant.issued')!
    expect(issued).toMatchObject({ presence: 'touchid', actor: { kind: 'person', id: 'p_sev' } })
    expect(started()[0].grant).toBe(issued.grant)
  })
  it('a member with no grant cannot sign one: the dialog says who can, and Start agent is disabled', async () => {
    const { user } = renderApp('/ticket/DEMO-0048', {
      setup: (s) => {
        s.appendWs(wsOf(s), { type: 'member.added', person: 'p_lea', name: 'Lea', role: 'member' })
        s.setViewer('p_lea')
      },
    })
    await openTicketPanel(user, 'Start agent')
    await user.click(await screen.findByRole('button', { name: 'Start' }, FIRST))
    const dialog = await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0048' }, T)
    expect(within(dialog).getByText(/You have no active grant in this workspace/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Start agent' })).toBeDisabled()
  })
  it('a viewer sees the panel read only', async () => {
    const { user } = renderApp('/ticket/DEMO-0048', { viewer: 'p_tom' })
    await openTicketPanel(user, 'Start agent')
    expect(await screen.findByRole('button', { name: 'Start' }, FIRST)).toBeDisabled()
  })
  it('while a run is on, the rail shows it and Stop ends it and releases the claim', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev', setup: playRun(4000) })
    await openTicketPanel(user, 'Start agent')
    expect(await screen.findByText(/T1 Create new service principal/, {}, FIRST)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(mockStore.ticket('DEMO-0044')!.claim).toBeNull(), T)
    expect(mockStore.sim.running()).toEqual([])
  })
})

describe('the run is visible live across the app', { timeout: 20_000 }, () => {
  it('Today: the blocking question lands in Needs you for the viewer and the agent is at work', async () => {
    const { user } = renderApp('/', { viewer: 'p_sev', setup: playRun(15_000) })
    expect(await screen.findByText(/T1 is done\. Go on with T2/, {}, FIRST)).toBeInTheDocument()
    await user.click(within(screen.getByRole('region', { name: 'Agents' })).getByRole('button', { name: 'Show' }))
    const atWork = await screen.findByRole('dialog', { name: 'Agents' })
    expect(within(atWork).getByText('Rotate warehouse service credentials')).toBeInTheDocument()
  })
  it('Agents: the session is listed, waiting on you', async () => {
    renderApp('/agents', { viewer: 'p_sev', setup: playRun(15_000) })
    const waiting = await screen.findByRole('region', { name: /Waiting on you/ }, FIRST)
    expect(within(waiting).getByText('DEMO-0044')).toBeInTheDocument()
  })
  it('the Start agent page lists the run with Stop, and the ticket form', async () => {
    renderApp('/addon/start-agent/start', { viewer: 'p_sev', setup: playRun(2000) })
    const table = await screen.findByRole('table', {}, FIRST)
    expect(within(table).getByText('DEMO-0044')).toBeInTheDocument()
    expect(within(table).getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /Ticket/ })).toBeInTheDocument()
  })
})

describe('start agent: precheck and a calmer confirm (G3)', { timeout: 20_000 }, () => {
  it('the panel previews with "Preview command", and Refine runs /orch:refine', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, 'Start agent')
    await user.selectOptions(await within(panel).findByLabelText(/Mode/, {}, FIRST), 'Refine')
    await user.click(within(panel).getByRole('button', { name: 'Preview command' }))
    expect(await within(panel).findByText(code("orch session start --in background DEMO-0044 -- claude '/orch:refine DEMO-0044'"), {}, T)).toBeInTheDocument()
  })

  it('the confirm warns when gates are not approved and folds the grant id and command under Details', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    const panel = await openTicketPanel(user, 'Start agent')
    await user.click(await within(panel).findByRole('button', { name: 'Start' }, FIRST))
    const dialog = await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0044' }, T)
    expect(within(dialog).getByRole('note')).toHaveTextContent('The plan is not approved yet. The agent starts by refining it.')
    const details = within(dialog).getByText('Details').closest('details')!
    expect(details).not.toHaveAttribute('open')
    expect(within(details).getByText(/gr_01J9Z8/)).toBeInTheDocument()
    expect(within(details).getByLabelText('Command')).toBeInTheDocument()
    expect(within(within(dialog).getByLabelText('What orch will start')).queryByText(/gr_01J9Z8/)).toBeNull()
  })

  it('the Start agent page preselects a ticket and never says "Pick a ticket" next to it', async () => {
    renderApp('/addon/start-agent/start', { viewer: 'p_sev' })
    expect(await screen.findByRole('combobox', { name: /Ticket/ }, FIRST)).not.toHaveValue('')
    expect(screen.queryByText(/Pick a ticket/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Preview command' })).toBeInTheDocument()
  })
})
