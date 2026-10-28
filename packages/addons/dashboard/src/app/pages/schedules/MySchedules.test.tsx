import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ScheduleRow } from '@/api/schedules'
import MySchedules, { nextRunText } from './MySchedules'

const h = vi.hoisted(() => ({ signed: [] as unknown[], show: vi.fn(), rows: [] as unknown[], cap: undefined as number | null | undefined, lead: undefined as number | null | undefined }))

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
  useSchedules: () => ({ data: { available: true, role: 'member', can: { add: true, cancel: true }, grant_hours_cap: h.cap, min_lead_s: h.lead, rows: h.rows }, isLoading: false }),
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

// A fixed clock (Wed 28 Oct 2026, 06:00 UTC; the suite runs in UTC): what the form offers depends on now plus the lead.
const NOW = new Date('2026-10-28T06:00:00Z')

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  h.signed.length = 0
  h.show.mockClear()
  h.rows = [WEEKLY, ONCE, OTHER, GONE]
  h.cap = undefined
  h.lead = undefined
})

afterEach(() => {
  vi.useRealTimers()
})

async function openForm() {
  const user = userEvent.setup()
  view()
  fireEvent.click(screen.getByRole('button', { name: 'New schedule' }))
  const d = within(screen.getByRole('dialog'))
  fireEvent.click(d.getByRole('radio', { name: 'An existing ticket' }))
  await user.click(d.getByRole('combobox', { name: 'Ticket' }))
  await user.click(await screen.findByRole('option', { name: 'DEMO-1 Import' }))
  await waitFor(() => expect(d.getByRole('combobox', { name: 'Ticket' }).textContent).toContain('DEMO-1 Import'))
  fireEvent.change(d.getByLabelText('What should the agent do'), { target: { value: 'Rebuild the forecast.' } })
  return d
}

describe('New schedule and the minimum lead (run 13)', () => {
  it('starts a once-run no sooner than now plus the lead, and signs that time', async () => {
    vi.setSystemTime(new Date('2026-10-28T08:20:00Z')) // 09:00 today is only 40 minutes away
    const d = await openForm()
    expect(d.getByRole('button', { name: 'Time: 09:30' })).toBeTruthy() // 08:20 + 1 h, up to the next quarter
    expect(d.queryByRole('alert')).toBeNull()
    fireEvent.click(d.getByRole('button', { name: 'Time: 09:30' }))
    expect((screen.getByRole('button', { name: '15 minutes earlier' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(d.getByRole('button', { name: 'Create schedule' }))
    fireEvent.click(screen.getByRole('button', { name: /^Sign: Schedule on DEMO-1/ }))
    expect(h.signed).toEqual([expect.objectContaining({ action: 'schedule_add', at: '2026-10-28 09:30', tz: 'UTC' })])
  })

  it('reads the lead from the server: a dev sandbox lead keeps 09:00 today', async () => {
    vi.setSystemTime(new Date('2026-10-28T08:50:00Z'))
    h.lead = 30
    const d = await openForm()
    expect(d.getByRole('button', { name: 'Time: 09:00' })).toBeTruthy()
    expect((d.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('says in plain words when a repeating rule would first run too soon, and does not offer to sign it', async () => {
    vi.setSystemTime(new Date('2026-10-28T08:30:00Z')) // a Wednesday
    const d = await openForm()
    fireEvent.click(d.getByRole('button', { name: 'Every week' }))
    fireEvent.click(d.getByRole('button', { name: 'Wed' })) // on
    fireEvent.click(d.getByRole('button', { name: 'Mon' })) // off
    expect(d.getByText('Runs every week on Wed at 09:30')).toBeTruthy() // today at 09:30: exactly the lead, allowed
    expect(d.queryByRole('alert')).toBeNull()
    fireEvent.click(d.getByRole('button', { name: 'Time: 09:30' }))
    fireEvent.click(screen.getByRole('button', { name: '15 minutes earlier' })) // a time of day is free on a repeating rule
    expect(d.getByText('Runs every week on Wed at 09:15')).toBeTruthy() // its first run, today at 09:15, is too soon
    expect(d.getByRole('alert').textContent).toContain('A schedule must start at least 1 hour from now.')
    expect((d.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(d.getByRole('button', { name: 'Thu' }))
    fireEvent.click(d.getByRole('button', { name: 'Wed' })) // off again: the first run is Thursday
    expect(d.queryByRole('alert')).toBeNull()
    expect((d.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('MySchedules', () => {
  it('defaults to new tickets and requires a title as well as the brief', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: 'New schedule' }))
    const d = within(screen.getByRole('dialog'))
    expect((d.getByRole('radio', { name: 'A new ticket each run' }) as HTMLInputElement).checked).toBe(true)
    expect(d.queryByRole('combobox', { name: 'Ticket' })).toBeNull()
    const create = d.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement
    fireEvent.change(d.getByLabelText('What should the agent do'), { target: { value: 'Check for mail.' } })
    expect(create.disabled).toBe(true)
    const title = d.getByLabelText('Ticket title') as HTMLInputElement
    expect(title.maxLength).toBe(100)
    fireEvent.change(title, { target: { value: '   ' } })
    expect(create.disabled).toBe(true)
    fireEvent.change(title, { target: { value: 'Check the shared mailbox' } })
    expect(create.disabled).toBe(false)
    fireEvent.click(create)
    fireEvent.click(screen.getByRole('button', { name: 'Sign: A new ticket each run' }))
    expect(h.signed).toEqual([expect.objectContaining({ action: 'schedule_add', target: 'new', title: 'Check the shared mailbox' })])
    expect(h.signed[0]).not.toHaveProperty('key')
  })

  it('requires a picked ticket when switching to existing', () => {
    view()
    fireEvent.click(screen.getByRole('button', { name: 'New schedule' }))
    const d = within(screen.getByRole('dialog'))
    fireEvent.change(d.getByLabelText('Ticket title'), { target: { value: 'Mailbox' } })
    fireEvent.change(d.getByLabelText('What should the agent do'), { target: { value: 'Check mail.' } })
    fireEvent.click(d.getByRole('radio', { name: 'An existing ticket' }))
    expect((d.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement).disabled).toBe(true)
    expect(d.queryByLabelText('Ticket title')).toBeNull()
  })

  it('restores the new-ticket target when undoing cancellation', async () => {
    h.rows = [{ ...WEEKLY, target: 'new', title: 'Mailbox' }]
    view()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: /^Sign: Cancel the schedule/ }))
    h.show.mock.calls[0][1].action.onClick()
    fireEvent.click(await screen.findByRole('button', { name: 'Sign: Restore schedule: A new ticket each run' }))
    expect(h.signed[1]).toEqual(expect.objectContaining({ target: 'new', title: 'Mailbox' }))
    expect(h.signed[1]).not.toHaveProperty('key')
  })

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
    fireEvent.click(d.getByRole('radio', { name: 'An existing ticket' }))
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

  it('an unsupported row is one quiet line and does not hide the others', () => {
    h.rows = [row({ id: 'sc_x', supported: false, note: 'Needs a newer dashboard' } as never), row({ id: 'sc_y', rule: { every: 'day', at: '09:00', tz: 'UTC' } })]
    view()
    expect(screen.getByText('Needs a newer dashboard')).toBeInTheDocument()
    expect(screen.getAllByText('DEMO-1').length).toBeGreaterThan(0)
  })
})
