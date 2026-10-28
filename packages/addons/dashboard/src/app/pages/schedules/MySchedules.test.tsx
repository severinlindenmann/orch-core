import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ScheduleRow } from '@/api/schedules'
import MySchedules, { nextRunText } from './MySchedules'

const h = vi.hoisted(() => ({ signed: [] as unknown[], show: vi.fn(), rows: [] as unknown[], cap: undefined as number | null | undefined }))

vi.mock('@/components/sign/SignPopover', () => ({
  SignPopover: (p: { action: unknown; title: string; onSettled?: (phase: string) => void }) => (
    <button
      onClick={() => {
        h.signed.push(p.action)
        p.onSettled?.('done')
      }}
    >
      {`Sign: ${p.title}`}
    </button>
  ),
}))
vi.mock('sonner', () => ({ toast: h.show }))
vi.mock('@tanstack/react-router', () => ({ Link: (p: { children: React.ReactNode }) => <a>{p.children}</a> }))
vi.mock('@/api/client', () => ({ api: { listTickets: vi.fn(async () => []) } }))
vi.mock('@/api/schedules', async (orig) => ({
  ...(await orig<typeof import('@/api/schedules')>()),
  useSchedules: () => ({ data: { available: true, role: 'member', can: { add: true, cancel: true }, grant_hours_cap: h.cap, rows: h.rows }, isLoading: false }),
}))

const row = (over: Partial<ScheduleRow> & Pick<ScheduleRow, 'id' | 'rule'>): ScheduleRow => ({
  ticket: 'DEMO-1',
  title: 'Import',
  ends: '2026-11-29T09:00:00Z',
  brief: 'Check the import.',
  label: null,
  run_hours: 3,
  state: 'live',
  mine: true,
  can_cancel: true,
  ...over,
})

const WEEKLY = row({ id: 's1', rule: { every: 'week', at: '09:00', tz: 'UTC', weekdays: [0, 3] } })
const ONCE = row({ id: 's2', rule: { at: '12:00', tz: 'UTC', date: '2026-11-02' } })
const OTHER = row({ id: 's3', mine: false, rule: { every: 'month', at: '09:00', tz: 'UTC', day: 5 } })
const GONE = row({ id: 's4', state: 'cancelled', rule: { every: 'day', at: '08:00', tz: 'UTC' } })

function view(manager = false) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <MySchedules ws="w1" manager={manager} tickets={[{ key: 'DEMO-1', title: 'Import' }]} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  h.signed.length = 0
  h.show.mockClear()
  h.rows = [WEEKLY, ONCE, OTHER, GONE]
  h.cap = undefined
})

describe('MySchedules', () => {
  it('renders my rows with their summaries and hides other people for a member', () => {
    view()
    expect(screen.getByText('Runs every week on Mon and Thu at 09:00')).toBeTruthy()
    expect(screen.getByText('Runs once on Mon 2 Nov at 12:00')).toBeTruthy()
    expect(screen.queryByText('Runs every month on day 5 at 09:00')).toBeNull()
    expect(screen.getAllByText(/Next run/).length).toBeGreaterThan(0)
  })

  it('shows other people to a manager', () => {
    view(true)
    expect(screen.getByText('Runs every month on day 5 at 09:00')).toBeTruthy()
  })

  it('reads the next run on the rule clock', () => {
    expect(nextRunText(WEEKLY, new Date('2026-10-30T12:00:00Z'))).toBe('Next run Mon 2 Nov, 09:00')
    expect(nextRunText(GONE, new Date('2026-10-30T12:00:00Z'))).toBeNull()
  })

  it('shows the server next run on the rule clock, and nothing when the server says it is over', () => {
    h.rows = [
      row({ id: 's6', next_run: '2026-11-03T14:30:00Z', rule: { every: 'day', at: '09:00', tz: 'Europe/Berlin' } }),
      row({ id: 's7', next_run: null, rule: { every: 'day', at: '09:00', tz: 'UTC' } }),
    ]
    view()
    expect(screen.getByText('Next run Tue 3 Nov, 15:30')).toBeTruthy()
    expect(screen.getAllByText(/Next run/)).toHaveLength(1)
  })

  it('says in plain words why the last run was skipped', () => {
    const skipped = (id: string, reason: string | null) => row({ id, next_run: null, rule: { every: 'day', at: '08:00', tz: 'UTC' }, last_outcome: { at: '2026-11-02T08:00:00Z', outcome: 'did_not_run', reason } })
    h.rows = [skipped('a', 'busy'), skipped('b', 'claimed_by_other'), skipped('c', 'access_lost'), skipped('d', 'addon_disabled'), skipped('e', 'something_new'), skipped('f', null)]
    h.rows.push(row({ id: 'g', next_run: null, rule: { every: 'day', at: '08:00', tz: 'UTC' }, last_outcome: { at: '2026-11-02T08:00:00Z', outcome: 'ran', reason: null } }))
    view()
    expect(screen.getByText('Skipped last time: you had work running on this ticket.')).toBeTruthy()
    expect(screen.getByText('Skipped last time: someone else was working on this ticket.')).toBeTruthy()
    expect(screen.getByText('Skipped last time: you no longer have access to this ticket.')).toBeTruthy()
    expect(screen.getByText('Skipped last time: Schedules was turned off.')).toBeTruthy()
    expect(screen.getAllByText('Skipped last time.')).toHaveLength(2)
    expect(screen.getAllByText(/Skipped last time/)).toHaveLength(6)
  })

  it('names the real number of hours in the New schedule dialog', () => {
    const copy = () => screen.getByText(/An agent starts by itself each time/).textContent
    const { unmount } = view()
    fireEvent.click(screen.getByRole('button', { name: 'New schedule' }))
    expect(copy()).toContain('for up to 8 hours.')
    unmount()
    h.cap = 3
    view()
    fireEvent.click(screen.getByRole('button', { name: 'New schedule' }))
    expect(copy()).toContain('for up to 3 hours.')
  })

  it('builds the add action for a weekly schedule with two weekdays', async () => {
    const user = userEvent.setup()
    view()
    fireEvent.click(screen.getByRole('button', { name: 'New schedule' }))
    const dialog = screen.getByRole('dialog')
    const d = within(dialog)
    // The ticket is the shared Dropdown (radix Select): open the combobox, press the option.
    await user.click(d.getByRole('combobox', { name: 'Ticket' }))
    await user.click(await screen.findByRole('option', { name: 'DEMO-1 Import' }))
    await waitFor(() => expect(d.getByRole('combobox', { name: 'Ticket' }).textContent).toContain('DEMO-1 Import'))
    expect(d.getByRole('button', { name: 'Time: 09:00' })).toBeTruthy()
    fireEvent.change(d.getByLabelText('What should the agent do'), { target: { value: 'Rebuild the forecast.' } })
    fireEvent.click(d.getByRole('button', { name: 'Every week' }))
    fireEvent.click(d.getByRole('button', { name: 'Thu' }))
    expect(d.getByText('Runs every week on Mon and Thu at 09:00')).toBeTruthy()
    fireEvent.click(d.getByRole('button', { name: 'Create schedule' }))
    fireEvent.click(screen.getByRole('button', { name: /^Sign: Schedule on DEMO-1/ }))
    expect(h.signed).toEqual([expect.objectContaining({ action: 'schedule_add', key: 'DEMO-1', brief: 'Rebuild the forecast.', at: '09:00', every: 'week', weekdays: [0, 3] })])
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps Create schedule off until a ticket and a brief are set', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: 'New schedule' }))
    const d = within(screen.getByRole('dialog'))
    const create = d.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement
    expect(create.disabled).toBe(true)
    expect(d.getByRole('button', { name: 'Date: Today' })).toBeTruthy() // Once starts on today, in the shared DatePicker
  })

  it('cancels a schedule and offers Undo that adds it again', async () => {
    view()
    fireEvent.click(screen.getAllByRole('button', { name: 'Cancel' })[0])
    fireEvent.click(screen.getByRole('button', { name: /^Sign: Cancel the schedule/ }))
    expect(h.signed).toEqual([{ action: 'schedule_cancel', key: 'DEMO-1', id: 's1' }])
    const undo = h.show.mock.calls[0][1].action
    expect(undo.label).toBe('Undo')
    undo.onClick()
    fireEvent.click(await screen.findByRole('button', { name: /^Sign: Restore the schedule/ }))
    expect(h.signed[1]).toEqual(expect.objectContaining({ action: 'schedule_add', key: 'DEMO-1', every: 'week', weekdays: [0, 3], at: '09:00', run_hours: 3 }))
  })

  it('has no Cancel where the server says the viewer may not cancel', () => {
    h.rows = [row({ id: 's5', mine: false, can_cancel: false, rule: { every: 'day', at: '08:00', tz: 'UTC' } })]
    view(true)
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
  })

  it('has no Cancel on a cancelled schedule', () => {
    h.rows = [GONE]
    view()
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    expect(screen.getByText(/Cancelled/)).toBeTruthy()
  })
})
