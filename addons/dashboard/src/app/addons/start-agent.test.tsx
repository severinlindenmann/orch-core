import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import type { MockStore } from '@/mocks/store'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
const wsOf = (s: MockStore) => s.workspaces.find((w) => w.prefix === 'DEMO')!.id
const started = () => mockStore.wsEventsOf(wsOf(mockStore)).filter((e) => e.type === 'agent.started')
const COMMAND = 'orch session start --in background DEMO-0044 -- claude "/orch:work DEMO-0044"'
/** A code block whose text is exactly `text` (highlighting splits it into spans). */
const code = (text: string) => (_: string, el: Element | null) => el?.tagName === 'CODE' && el.textContent === text
/** Start on DEMO-0044 the way core does after its dialog, then play the run on fake timers up to `ms`. */
const playRun = (ms: number) => (s: MockStore) => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }) // the mock clock (Date) keeps running
  const res = s.runAddon(wsOf(s), 'start-agent', 'start', { ticket: 'DEMO-0044', confirmed: true })
  if (!res?.ok) throw new Error('start failed')
  vi.advanceTimersByTime(ms)
  vi.useRealTimers()
}

afterEach(() => vi.useRealTimers())

describe('start agent on the ticket rail', () => {
  it('shows the exact command; Start opens orch\'s own dialog, and confirming there starts the run', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    expect(await screen.findByText(code(COMMAND), {}, T)).toBeInTheDocument()
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
  it('Cancel starts nothing', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev' })
    await user.click(await screen.findByRole('button', { name: 'Start' }, T))
    await user.click(within(await screen.findByRole('dialog', {}, T)).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), T)
    expect(started()).toHaveLength(0)
  })
  it('with no active grant the dialog is core\'s signing prompt: signing issues a grant (Touch ID) and starts', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', {
      viewer: 'p_sev',
      setup: (s) => void s.revokeGrant(wsOf(s), 'gr_01J9Z8', { kind: 'person', id: 'p_sev' }),
    })
    await user.click(await screen.findByRole('button', { name: 'Start' }, T))
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
    await user.click(await screen.findByRole('button', { name: 'Start' }, T))
    const dialog = await screen.findByRole('dialog', { name: 'Start Claude Code on DEMO-0048' }, T)
    expect(within(dialog).getByText(/You have no active grant in this workspace/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Start agent' })).toBeDisabled()
  })
  it('a viewer sees the panel read only', async () => {
    renderApp('/ticket/DEMO-0048', { viewer: 'p_tom' })
    expect(await screen.findByRole('button', { name: 'Start' }, T)).toBeDisabled()
  })
  it('while a run is on, the rail shows it and Stop ends it and releases the claim', async () => {
    const { user } = renderApp('/ticket/DEMO-0044', { viewer: 'p_sev', setup: playRun(4000) })
    expect(await screen.findByText(/T1 Create new service principal/, {}, T)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(mockStore.ticket('DEMO-0044')!.claim).toBeNull(), T)
    expect(mockStore.sim.running()).toEqual([])
  })
})

describe('the run is visible live across the app', () => {
  it('Today: the blocking question lands in Needs you for the viewer and the agent is at work', async () => {
    renderApp('/', { viewer: 'p_sev', setup: playRun(15_000) })
    expect(await screen.findByText(/T1 is done\. Go on with T2/, {}, T)).toBeInTheDocument()
    const atWork = screen.getByRole('region', { name: 'Agents at work' })
    expect(within(atWork).getByText('Rotate warehouse service credentials')).toBeInTheDocument()
  })
  it('Agents: the session is listed, waiting on you', async () => {
    renderApp('/agents', { viewer: 'p_sev', setup: playRun(15_000) })
    const session = started()[0]?.session as string
    const tree = await screen.findByRole('tree', { name: 'Sessions' }, T)
    const item = within(tree).getByRole('treeitem', { name: new RegExp(session) })
    expect(within(item).getByText('waiting')).toBeInTheDocument()
    expect(within(item).getByText('waiting on you')).toBeInTheDocument()
  })
  it('the Start agent page lists the run with Stop, and the ticket form', async () => {
    renderApp('/addon/start-agent/start', { viewer: 'p_sev', setup: playRun(2000) })
    const table = await screen.findByRole('table', {}, T)
    expect(within(table).getByText('DEMO-0044')).toBeInTheDocument()
    expect(within(table).getByRole('button', { name: 'Stop' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /Ticket/ })).toBeInTheDocument()
  })
})
