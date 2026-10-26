import { act, screen, waitFor, within } from '@testing-library/react'
import { createElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', async (orig) => {
  const actual = await orig<typeof import('sonner')>()
  return { ...actual, toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }
})
// jsdom has no layout for real dragging: capture the board's drag-end handler.
const dnd = vi.hoisted(() => ({ onDragEnd: undefined as undefined | ((e: unknown) => void) }))
vi.mock('@dnd-kit/core', async (orig) => {
  const actual = await orig<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: Parameters<typeof actual.DndContext>[0]) => {
      dnd.onDragEnd = props.onDragEnd as (e: unknown) => void
      return createElement(actual.DndContext, props)
    },
  }
})

import { api, mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
const DISPLAY = 'orch.board.display.p_sev'

describe('board grouped by epic (N2)', () => {
  beforeEach(() => {
    Element.prototype.setPointerCapture = () => {}
  })

  it('groups by epic by default: an epic lane with its children, no epic card, and a No epic lane', async () => {
    renderApp('/board')
    const lane = await screen.findByRole('button', { name: /^Collapse DEMO-0040 /, expanded: true }, T)
    expect(lane).toBeInTheDocument()
    expect(screen.queryByTestId('card-DEMO-0040')).toBeNull()
    expect(screen.getByRole('button', { name: /^Collapse No epic/ })).toBeInTheDocument()
    expect(within(screen.getByRole('group', { name: 'DEMO-0040 · In progress' })).getByTestId('card-DEMO-0043')).toBeInTheDocument()
    // a single ticket keeps its normal card; the child is the compact two-line card
    expect(screen.getByTestId('card-DEMO-0044').closest('[role=group]')).toHaveAttribute('aria-label', expect.stringMatching(/^No epic · /))
    expect(screen.getByRole('img', { name: /^DEMO-0040 .*: \d+\/4 done$/ })).toBeInTheDocument()
  })

  it('Display has Group: Epic / None; None restores the flat columns, and the choice is remembered', async () => {
    const { user, unmount } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    await user.click(screen.getByRole('button', { name: 'Display' }))
    const group = await screen.findByRole('radiogroup', { name: 'Group by' })
    expect(within(group).getByRole('radio', { name: 'Epic' })).toBeChecked()
    await user.click(within(group).getByRole('radio', { name: 'None' }))
    // flat: the epic is a card again and the columns hold their cards directly
    expect(await screen.findByTestId('card-DEMO-0040')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'In progress' })).getByTestId('card-DEMO-0043')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Collapse DEMO-0040/ })).toBeNull()
    expect(JSON.parse(localStorage.getItem(DISPLAY)!).group).toBe('none')
    unmount()
    renderApp('/board', { storage: { [DISPLAY]: localStorage.getItem(DISPLAY)! } })
    expect(await screen.findByTestId('card-DEMO-0040', {}, T)).toBeInTheDocument()
  })

  it('grouped, a column sums only its cards; the epic\'s own points show in its lane header, labelled own', async () => {
    const { user } = renderApp('/board')
    const sumOf = async () => Number(/(\d+) pts?$/.exec((await within(await screen.findByRole('region', { name: 'In progress' })).findByLabelText(/^Sum of /)).textContent ?? '')?.[1])
    const grouped = await sumOf()
    const lane = (await screen.findByRole('button', { name: /^Collapse DEMO-0040 / })).closest('div.sticky')!
    expect(lane).toHaveTextContent(/own\s*A?\s*21\s*pt/)
    await user.click(screen.getByRole('button', { name: 'Display' }))
    await user.click(await screen.findByRole('radio', { name: 'None' }))
    // flat, the epic is a card in its column and its 21 points are in the sum
    await waitFor(async () => expect(await sumOf()).toBe(grouped + 21))
  })

  it('No epic comes last, and lane titles are h3 headings', async () => {
    renderApp('/board')
    const none = await screen.findByRole('button', { name: /^Collapse No epic/ }, T)
    const epic = screen.getByRole('button', { name: /^Collapse DEMO-0040 / })
    expect(epic.compareDocumentPosition(none) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByRole('heading', { level: 3, name: 'No epic' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: /Tariff and billing/ })).toBeInTheDocument()
  })

  it('the toolbar counts tickets, not epics, when grouped', async () => {
    const { user } = renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    expect(screen.getByText('17 tickets')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Display' }))
    await user.click(await screen.findByRole('radio', { name: 'None' }))
    expect(await screen.findByText('19 tickets')).toBeInTheDocument()
  })

  it('is remembered per viewer: Tom does not inherit Severin\'s choice, and Tom can fold a lane', async () => {
    const { user } = renderApp('/board', { viewer: 'p_tom', storage: { [DISPLAY]: JSON.stringify({ group: 'none' }) } })
    const fold = await screen.findByRole('button', { name: /^Collapse DEMO-0040 /, expanded: true }, T)
    expect(screen.queryByTestId('card-DEMO-0040')).toBeNull()
    await user.click(fold)
    expect(await screen.findByRole('button', { name: /^Expand DEMO-0040 /, expanded: false })).toBeInTheDocument()
    expect(JSON.parse(localStorage.getItem('orch.board.display.p_tom')!).lanes['DEMO-0040']).toBe(true)
    expect(JSON.parse(localStorage.getItem(DISPLAY)!).group).toBe('none')
  })

  it('a search hit inside a big epic is shown, not hidden in the fold', async () => {
    const { user } = renderApp('/board', { viewer: 'p_sev', setup: (s) => s.reset('busy') })
    await screen.findByRole('button', { name: /^Expand DEMO-0100 /, expanded: false }, T)
    const kid = mockStore.ticket(mockStore.ticket('DEMO-0100')!.children![7])!
    await user.type(screen.getByLabelText('Filter tickets'), kid.title)
    expect(await screen.findByTestId(`card-${kid.key}`, {}, T)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Collapse DEMO-0100 /, expanded: true })).toBeInTheDocument()
    mockStore.sim.stopAll()
  }, 30_000)

  it('a big epic (40 children) starts folded: one lane header, no child cards; Enter unfolds, the arrow keys fold and unfold', async () => {
    const { user } = renderApp('/board', { viewer: 'p_sev', setup: (s) => s.reset('busy') })
    const folded = await screen.findByRole('button', { name: /^Expand DEMO-0100 /, expanded: false }, T)
    const children = mockStore.ticket('DEMO-0100')!.children!
    expect(children).toHaveLength(40)
    for (const k of children) expect(screen.queryByTestId(`card-${k}`)).toBeNull()
    expect(screen.getByText(/\d+\/40 done/)).toBeInTheDocument()
    folded.focus()
    await user.keyboard('{Enter}')
    const open = await screen.findByRole('button', { name: /^Collapse DEMO-0100 / })
    expect(open).toHaveAttribute('aria-expanded', 'true')
    expect(JSON.parse(localStorage.getItem(DISPLAY)!).lanes['DEMO-0100']).toBe(false)
    await waitFor(() => expect(screen.getAllByTestId(/^card-/).length).toBeGreaterThan(5), T)
    open.focus()
    await user.keyboard('{ArrowLeft}')
    expect(await screen.findByRole('button', { name: /^Expand DEMO-0100 / })).toHaveAttribute('aria-expanded', 'false')
    screen.getByRole('button', { name: /^Expand DEMO-0100 / }).focus()
    await user.keyboard('{ArrowRight}')
    expect(await screen.findByRole('button', { name: /^Collapse DEMO-0100 / })).toBeInTheDocument()
    mockStore.sim.stopAll()
  }, 30_000)

  it('only the card\'s own lane takes a drop: another epic\'s cell is not a target and a drop there does nothing', async () => {
    renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    act(() => dnd.onDragEnd!({ active: { data: { current: { ticket: { key: 'DEMO-0043', status: 'in-progress' } } } }, over: { id: 'DEMO-0050|open' } }))
    await new Promise((r) => setTimeout(r, 300))
    expect(within(screen.getByRole('group', { name: 'DEMO-0040 · In progress' })).getByTestId('card-DEMO-0043')).toBeInTheDocument()
    expect(mockStore.ticket('DEMO-0043')!.status).toBe('in-progress')
  })

  it('dropping a child on another status cell of its lane moves its status', async () => {
    renderApp('/board')
    await screen.findByTestId('card-DEMO-0043', {}, T)
    act(() => dnd.onDragEnd!({ active: { data: { current: { ticket: { key: 'DEMO-0043', status: 'in-progress' } } } }, over: { id: 'DEMO-0040|open' } }))
    const cell = screen.getByRole('group', { name: 'DEMO-0040 · Open' })
    await waitFor(() => expect(within(cell).getByTestId('card-DEMO-0043')).toBeInTheDocument(), T)
  })

  it('if the viewer cannot be loaded the board shows an error with Retry, not Loading forever', async () => {
    const real = api.getMe.bind(api)
    // Down until restored: the route loader asks first, the page asks again when it mounts (G4).
    const spy = vi.spyOn(api, 'getMe').mockRejectedValue(new Error('down'))
    const { user } = renderApp('/board')
    expect(await screen.findByRole('alert', {}, T)).toHaveTextContent(/Could not load the board/)
    spy.mockImplementation(real)
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('card-DEMO-0043', {}, T)).toBeInTheDocument()
    spy.mockRestore()
  })
})
