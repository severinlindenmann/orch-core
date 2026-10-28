// "My schedules" (U7): the person's scheduled runs, a Cancel with Undo, and the "New schedule" form.
import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { toast } from 'sonner'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { DEFAULT_MIN_LEAD_S, firstSlot, leadSentence, scheduleAddAction, scheduleKey, scheduleSummary, skippedText, startsTooSoon, useSchedules, type ScheduleForm, type ScheduleRepeat, type ScheduleRow } from '@/api/schedules'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Raw } from '@/components/sign/visible'
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

/** "Next run Mon 2 Nov, 09:00" from the server's instant, read on the rule's own clock (UTC if the zone is unknown). */
export function serverNextRunText(iso: string, tz: string): string | null {
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return null
  const opts: Intl.DateTimeFormatOptions = { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
  let parts: Intl.DateTimeFormatPart[]
  try {
    parts = new Intl.DateTimeFormat('en-GB', { ...opts, timeZone: tz }).formatToParts(when)
  } catch {
    parts = new Intl.DateTimeFormat('en-GB', { ...opts, timeZone: 'UTC' }).formatToParts(when)
  }
  const p = (type: string) => parts.find((x) => x.type === type)?.value ?? ''
  return `Next run ${p('weekday')} ${p('day')} ${p('month')}, ${p('hour')}:${p('minute')}`
}

/** Fallback only for a row whose `next_run` the server did not send: the next run worked out here from the rule alone,
 * which knows nothing of recorded runs; null when nothing is left to run. */
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
  target: row.target,
  title: row.title ?? undefined,
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
          title={row.target === 'new' ? 'Restore schedule: A new ticket each run' : `Restore the schedule on ${row.ticket}`}
          action={scheduleAddAction(formOf(row))}
          confirmLabel="Restore schedule"
          onSettled={(p) => p === 'done' && refresh()}
          onClose={() => setPhase('idle')}
        />
      )}
    </>
  )
}

function Row({ row, ws }: { row: ScheduleRow; ws: string }) {
  // The server's next run knows the signing lead and the recorded runs; the rule-only sum is for a row without one.
  const next = row.state !== 'live' ? null : row.next_run === undefined ? nextRunText(row) : row.next_run ? serverNextRunText(row.next_run, row.rule.tz) : null
  const skipped = skippedText(row.last_outcome)
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
          {skipped && <p className="text-[12px] text-text-muted">{skipped}</p>}
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

const DEFAULT_RUN_HOURS = 8

function NewScheduleDialog({ ws, tickets, maxHours, leadS, onClose }: { ws: string; tickets?: TicketOption[]; maxHours: number; leadS: number; onClose: () => void }) {
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
  const [target, setTarget] = useState<'new' | 'existing'>('new')
  const [title, setTitle] = useState('')
  const [ticket, setTicket] = useState('')
  const [brief, setBrief] = useState('')
  const [repeat, setRepeat] = useState<ScheduleRepeat>('once')
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  const now = new Date()
  const today = toDateStr(now)
  // run 13: nothing before now plus the host's minimum lead is offered (the host refuses it: schedule.denied)
  const slot = firstSlot(tz, now, leadS)
  const [date, setDate] = useState(() => (`${today} 09:00` >= `${slot.date} ${slot.time}` ? today : slot.date))
  const [time, setTime] = useState(() => (`${today} 09:00` >= `${slot.date} ${slot.time}` ? '09:00' : slot.time))
  const [weekdays, setWeekdays] = useState([0])
  const [day, setDay] = useState(1)
  const pickDate = (d: string) => {
    setDate(d)
    if (d === slot.date && time < slot.time) setTime(slot.time)
  }
  const form: ScheduleForm = { target, title, ticket, brief: brief.trim(), repeat, date: repeat === 'once' ? date : undefined, time, weekdays, day, tz }
  const summary = scheduleSummary({ every: repeat === 'once' ? undefined : repeat, at: time, tz, weekdays, day, date: repeat === 'once' ? date : undefined })
  const tooSoon = (repeat !== 'once' || /^\d{4}-\d\d-\d\d$/.test(date)) && (repeat !== 'week' || weekdays.length > 0) && startsTooSoon(form, now, leadS)
  const valid = (target === 'new' ? !!title.trim() && title.length <= 100 : !!ticket) && !!form.brief && brief.length <= MAX_BRIEF && (repeat !== 'once' || /^\d{4}-\d\d-\d\d$/.test(date)) && (repeat !== 'week' || weekdays.length > 0) && !tooSoon
  const label = 'text-[12px] font-medium text-text-muted'
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div role="dialog" aria-modal="true" aria-label="New schedule" className="max-h-full w-full max-w-lg space-y-4 overflow-y-auto rounded-lg border border-border bg-surface p-4">
        <h2 className="text-base font-semibold">New schedule</h2>
        <fieldset className="space-y-1">
          <legend className={label}>What should each run work on?</legend>
          <div className="flex flex-wrap gap-3 text-[13px]">
            <label className="flex items-center gap-1"><input type="radio" name="schedule-target" checked={target === 'new'} onChange={() => setTarget('new')} />A new ticket each run</label>
            <label className="flex items-center gap-1"><input type="radio" name="schedule-target" checked={target === 'existing'} onChange={() => setTarget('existing')} />An existing ticket</label>
          </div>
        </fieldset>
        {target === 'new' ? (
          <div className="space-y-1">
            <label htmlFor="sched-title" className={label}>Ticket title</label>
            <input id="sched-title" value={title} maxLength={100} placeholder="Check the shared mailbox" onChange={(e) => setTitle(e.target.value)} className="w-full rounded border border-border bg-transparent p-2 text-[13px]" />
            <p className="text-[12px] text-text-faint">Each run starts a fresh ticket titled <Raw>{title || 'Check the shared mailbox'}</Raw> and today's date. The agent can close it if there is nothing to do.</p>
            <InfoDrawer label="About new tickets" title="A new ticket each run">
              <p>Each run uses the date in the schedule’s time zone. The agent can work on and close that run’s ticket. If the previous ticket is still open, the next run is skipped. Cancelling the schedule stops future runs; tickets already created stay as they are.</p>
            </InfoDrawer>
          </div>
        ) : (
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
        )}
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
            <DatePicker id="sched-date" value={date} min={slot.date} onChange={pickDate} />
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
          <TimePicker id="sched-time" value={time} minTime={repeat === 'once' && date === slot.date ? slot.time : undefined} onChange={setTime} />
          <p className="text-[11px] text-text-faint">Time zone {tz}</p>
        </div>
        {tooSoon && (
          <p role="alert" className="text-[12px] text-danger">
            {leadSentence(leadS)} {repeat === 'once' ? 'Pick a later time.' : 'Its first run would be sooner; pick another time or day.'}
          </p>
        )}
        <p className="text-[13px] font-medium">{summary}</p>
        <p className="text-[12px] text-text-muted">An agent starts by itself each time, with your rights on this ticket, for up to {maxHours} {maxHours === 1 ? 'hour' : 'hours'}. It stops after 30 days.</p>
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
              title={target === 'new' ? 'A new ticket each run' : `Schedule on ${ticket}`}
              action={scheduleAddAction(form)}
              confirmLabel="Create schedule"
              minLeadS={leadS}
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
  // The default run length, cut to the member's grant_hours when the server says it.
  const cap = data?.grant_hours_cap
  const maxHours = typeof cap === 'number' && cap >= 1 ? Math.min(DEFAULT_RUN_HOURS, cap) : DEFAULT_RUN_HOURS
  // The host's minimum lead for this workspace (core's caps, via the server); an hour when the server does not say.
  const lead = data?.min_lead_s
  const leadS = typeof lead === 'number' && lead >= 0 ? lead : DEFAULT_MIN_LEAD_S
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
      {adding && <NewScheduleDialog ws={ws} tickets={tickets} maxHours={maxHours} leadS={leadS} onClose={() => setAdding(false)} />}
    </section>
  )
}
