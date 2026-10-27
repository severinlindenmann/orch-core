// "My schedules" (U7): the person's scheduled runs, a Cancel with Undo, and the "New schedule" form.
import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { toast } from 'sonner'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { scheduleAddAction, scheduleKey, scheduleSummary, useSchedules, type ScheduleForm, type ScheduleRepeat, type ScheduleRow } from '@/api/schedules'
import { SignPopover } from '@/components/sign/SignPopover'
import { DatePicker } from '@/components/pickers/DatePicker'
import { Dropdown } from '@/components/pickers/Dropdown'
import { TimePicker } from '@/components/pickers/TimePicker'
import { toDateStr } from '@/components/pickers/dates'
import { Chip, Stepper, WeekdayChips } from './pickers'

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const MAX_BRIEF = 4096

export interface TicketOption {
  key: string
  title: string
  state?: string
}

const minutesOf = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5))

/** The date and minute of day it is now in a time zone (the browser's own if the name is not known). */
function nowIn(tz: string, now: Date) {
  const opts: Intl.DateTimeFormatOptions = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
  let parts: Intl.DateTimeFormatPart[]
  try {
    parts = new Intl.DateTimeFormat('en-CA', { ...opts, timeZone: tz }).formatToParts(now)
  } catch {
    parts = new Intl.DateTimeFormat('en-CA', opts).formatToParts(now)
  }
  const n = (type: string) => Number(parts.find((p) => p.type === type)?.value)
  return { y: n('year'), m: n('month'), d: n('day'), min: n('hour') * 60 + n('minute') }
}

/** "Next run Mon 2 Nov, 09:00" for a live schedule, read on the rule's own clock; null when nothing is left to run. */
export function nextRunText(row: ScheduleRow, now = new Date()): string | null {
  if (row.state !== 'live') return null
  const { rule } = row
  const t = nowIn(rule.tz, now)
  const date = rule.date?.slice(0, 10)
  for (let i = 0; i < 400; i++) {
    if (i === 0 && minutesOf(rule.at) <= t.min) continue
    const d = new Date(Date.UTC(t.y, t.m - 1, t.d + i))
    if (row.ends && d.toISOString().slice(0, 10) > row.ends.slice(0, 10)) return null
    const dow = (d.getUTCDay() + 6) % 7
    const dom = d.getUTCDate()
    const last = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate()
    const hit =
      rule.every === 'day' ||
      (rule.every === 'week' && (rule.weekdays ?? []).includes(dow)) ||
      (rule.every === 'month' && (rule.day === dom || (dom === last && (rule.day ?? 0) > last))) ||
      (!rule.every && date === d.toISOString().slice(0, 10))
    if (hit) return `Next run ${DAYS[dow]} ${dom} ${MONTHS[d.getUTCMonth()]}, ${rule.at}`
  }
  return null
}

/** The form that would create this schedule again (Undo of a cancel). */
const formOf = (row: ScheduleRow): ScheduleForm => ({
  ticket: row.ticket,
  brief: row.brief,
  repeat: row.rule.every ?? 'once',
  date: row.rule.date?.slice(0, 10),
  time: row.rule.at,
  weekdays: row.rule.weekdays ?? [],
  day: row.rule.day ?? 1,
  tz: row.rule.tz,
  run_hours: row.run_hours,
})

function CancelButton({ row, ws }: { row: ScheduleRow; ws: string }) {
  const qc = useQueryClient()
  const [phase, setPhase] = useState<'idle' | 'cancel' | 'undo'>('idle')
  const refresh = () => void qc.invalidateQueries({ queryKey: scheduleKey(ws) })
  return (
    <>
      <button type="button" onClick={() => setPhase('cancel')} className="rounded border border-border px-2 py-1 text-[12px]">
        Cancel
      </button>
      {phase === 'cancel' && (
        <SignPopover
          ws={ws}
          title={`Cancel the schedule on ${row.ticket}`}
          action={{ action: 'schedule_cancel', key: row.ticket, id: row.id }}
          autoStart
          confirmLabel="Cancel schedule"
          doneToast=""
          onSettled={(p) => {
            if (p === 'done') {
              refresh()
              toast('Schedule cancelled.', { duration: 15_000, action: { label: 'Undo', onClick: () => setPhase('undo') } })
            }
          }}
          onClose={() => setPhase((x) => (x === 'cancel' ? 'idle' : x))}
        />
      )}
      {phase === 'undo' && (
        <SignPopover
          ws={ws}
          title={`Restore the schedule on ${row.ticket}`}
          action={scheduleAddAction(formOf(row))}
          autoStart
          confirmLabel="Restore schedule"
          onSettled={(p) => p === 'done' && refresh()}
          onClose={() => setPhase('idle')}
        />
      )}
    </>
  )
}

function Row({ row, ws }: { row: ScheduleRow; ws: string }) {
  const next = nextRunText(row)
  return (
    <li className="space-y-1 rounded-md border border-border p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-0.5">
          <p className="text-[13px] font-medium">{scheduleSummary(row.rule)}</p>
          <p className="text-[12px] text-text-muted">
            <Link to="/ticket/$key" params={{ key: row.ticket }} className="font-mono underline">
              {row.ticket}
            </Link>
            {row.title ? ` ${row.title}` : ''}
            {!row.mine ? ' · Set by someone else' : ''}
            {row.state !== 'live' ? ` · ${row.state === 'cancelled' ? 'Cancelled' : 'Finished'}` : ''}
          </p>
          {next && <p className="text-[12px] text-text-muted">{next}</p>}
          <p className="line-clamp-2 text-[12px] text-text-faint">{row.brief}</p>
        </div>
        {row.state === 'live' && row.can_cancel && <CancelButton row={row} ws={ws} />}
      </div>
    </li>
  )
}

const REPEATS: [ScheduleRepeat, string][] = [
  ['once', 'Once'],
  ['day', 'Every day'],
  ['week', 'Every week'],
  ['month', 'Every month'],
]

function NewScheduleDialog({ ws, tickets, onClose }: { ws: string; tickets?: TicketOption[]; onClose: () => void }) {
  const qc = useQueryClient()
  const found = useQuery({
    queryKey: ['schedule-tickets', ws],
    enabled: !tickets,
    queryFn: async () => {
      const list = (await api.listTickets(ws, { status: ['open', 'in-progress'] })) as unknown as TicketOption[]
      return list
    },
  })
  const options = tickets ?? found.data ?? []
  const [signing, setSigning] = useState(false)
  const [ticket, setTicket] = useState('')
  const [brief, setBrief] = useState('')
  const [repeat, setRepeat] = useState<ScheduleRepeat>('once')
  const today = toDateStr(new Date())
  const [date, setDate] = useState(today)
  const [time, setTime] = useState('09:00')
  const [weekdays, setWeekdays] = useState([0])
  const [day, setDay] = useState(1)
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const form: ScheduleForm = { ticket, brief: brief.trim(), repeat, date: repeat === 'once' ? date : undefined, time, weekdays, day, tz }
  const summary = scheduleSummary({ every: repeat === 'once' ? undefined : repeat, at: time, tz, weekdays, day, date: repeat === 'once' ? date : undefined })
  const valid = !!ticket && !!form.brief && brief.length <= MAX_BRIEF && (repeat !== 'once' || /^\d{4}-\d\d-\d\d$/.test(date)) && (repeat !== 'week' || weekdays.length > 0)
  const label = 'text-[12px] font-medium text-text-muted'
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div role="dialog" aria-modal="true" aria-label="New schedule" className="max-h-full w-full max-w-lg space-y-4 overflow-y-auto rounded-lg border border-border bg-surface p-4">
        <h2 className="text-base font-semibold">New schedule</h2>
        <div className="space-y-1">
          <label htmlFor="sched-ticket" className={label}>
            Ticket
          </label>
          {options.length ? (
            <Dropdown
              id="sched-ticket"
              ariaLabel="Ticket"
              value={ticket}
              onChange={setTicket}
              options={[{ value: '', label: 'Pick a ticket', disabled: true }, ...options.map((t) => ({ value: t.key, label: `${t.key} ${t.title}` }))]}
            />
          ) : (
            <p className="text-[12px] text-text-faint">No open tickets.</p>
          )}
        </div>
        <label className="block space-y-1">
          <span className={label}>What should the agent do</span>
          <textarea value={brief} maxLength={MAX_BRIEF} rows={3} onChange={(e) => setBrief(e.target.value)} className="w-full rounded border border-border bg-transparent p-2 text-[13px]" />
        </label>
        <div role="group" aria-labelledby="sched-repeat" className="space-y-1">
          <p id="sched-repeat" className={label}>
            Repeat
          </p>
          <div className="flex flex-wrap gap-1">
            {REPEATS.map(([value, name]) => (
              <Chip key={value} on={repeat === value} onClick={() => setRepeat(value)}>
                {name}
              </Chip>
            ))}
          </div>
        </div>
        {repeat === 'once' && (
          <div className="space-y-1">
            <label htmlFor="sched-date" className={label}>
              Day
            </label>
            <DatePicker id="sched-date" value={date} min={today} onChange={setDate} />
          </div>
        )}
        {repeat === 'week' && <WeekdayChips value={weekdays} onChange={setWeekdays} />}
        {repeat === 'month' && (
          <div role="group" aria-label="Day of the month" className="flex items-center gap-2 text-[13px]">
            <span>On day</span>
            <Stepper label="day" value={day} min={1} max={31} onChange={setDay} />
          </div>
        )}
        <div className="space-y-1">
          <label htmlFor="sched-time" className={label}>
            Time of day
          </label>
          <TimePicker id="sched-time" value={time} onChange={setTime} />
          <p className="text-[11px] text-text-faint">Time zone {tz}</p>
        </div>
        <p className="text-[13px] font-medium">{summary}</p>
        <p className="text-[12px] text-text-muted">An agent starts by itself each time, with your rights on this ticket, for up to 8 hours. It stops after 30 days.</p>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded border border-border px-3 py-1.5 text-[13px]">
            Close
          </button>
          <button type="button" disabled={!valid} onClick={() => setSigning(true)} className="rounded bg-brand px-3 py-1.5 text-[13px] font-medium text-white disabled:opacity-50">
            Create schedule
          </button>
          {signing && (
            <SignPopover
              ws={ws}
              title={`Schedule on ${ticket}`}
              action={scheduleAddAction(form)}
              autoStart
              confirmLabel="Create schedule"
              onSettled={(p) => {
                if (p === 'done') {
                  void qc.invalidateQueries({ queryKey: scheduleKey(ws) })
                  onClose()
                }
              }}
              onClose={() => setSigning(false)}
            />
          )}
        </div>
      </div>
    </div>
  )
}

interface Props {
  ws: string
  /** Owners and maintainers also see other people's schedules. */
  manager: boolean
  tickets?: TicketOption[]
}

export default function MySchedules({ ws, manager, tickets }: Props) {
  const { data, isLoading } = useSchedules(ws)
  const [adding, setAdding] = useState(false)
  const rows = (data?.rows ?? []).filter((r) => manager || r.mine)
  if (data?.available === false) return <p className="text-[13px] text-text-faint">Schedules is not turned on in this workspace.</p>
  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-[13px] text-text-muted">Runs that start by themselves, on a clock you set.</p>
        <button type="button" onClick={() => setAdding(true)} className="rounded bg-brand px-3 py-1.5 text-[13px] font-medium text-white">
          New schedule
        </button>
      </div>
      {isLoading ? (
        <p className="text-[13px] text-text-faint">Loading.</p>
      ) : rows.length ? (
        <ul className="space-y-2">
          {rows.map((r) => (
            <Row key={r.id} row={r} ws={ws} />
          ))}
        </ul>
      ) : (
        <p className="text-[13px] text-text-faint">No schedules yet.</p>
      )}
      {adding && <NewScheduleDialog ws={ws} tickets={tickets} onClose={() => setAdding(false)} />}
    </section>
  )
}
