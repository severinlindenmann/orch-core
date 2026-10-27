// The pieces every follow-up screen shares (#720): the data hook, the state badge and the three actions.
// Cancel, Run now as me and Reschedule act on the host's own records. What the host cannot do yet is said in `can` and the
// button is not drawn (never a button that fails). On the mock the actions run through the Schedules add-on's actions; on
// a real host Cancel is one signed click (core's review) and Run now is the ordinary Start agent dialog.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { addonStateKey } from '@/api/queries'
import { hasFeature, isRealHost } from '@/api/features'
import { followupQueries, followupsOn, kindOf, quickPicks, type FollowupRow, type Kind } from '@/api/followups'
import { toastApiError } from '@/app/toast'
import { useMe } from '@/app/useMe'
import { useWorkspace } from '@/app/workspace'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'
import { cn } from '@/lib/utils'
import { fmtExact, nowMs } from '@/lib/time'

/** The follow-ups of the current workspace, or nothing while the add-on is off or the host cannot say. */
export function useFollowups() {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const on = followupsOn(workspace)
  const q = useQuery({ ...followupQueries.list(ws ?? ''), enabled: !!ws && on })
  const me = useMe(ws)
  const data = q.data?.available ? q.data : undefined
  return { ws, on, query: q, data, rows: data?.rows ?? [], can: data?.can, person: me.data?.person, role: data?.role }
}

/** "Mara" for a person id; "someone" when the workspace does not list them. */
export function usePersonName() {
  const { workspace } = useWorkspace()
  return (id: string | null) => (id ? (workspace?.members.find((m) => m.person === id)?.name ?? 'someone') : 'someone')
}

export function useRefresh(ws: string | undefined) {
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: ['followups', ws] })
    void qc.invalidateQueries({ queryKey: addonStateKey(ws, 'schedules') })
    void qc.invalidateQueries({ queryKey: ['tickets', ws] })
  }
}

const BADGE: Record<Kind, { text: string; tone: string }> = {
  waiting: { text: 'Waiting', tone: 'border-info/40 bg-info-soft text-info' },
  needs_person: { text: 'Needs a person', tone: 'border-warning/40 bg-warning-soft text-warning' },
  running: { text: 'Running now', tone: 'border-info/40 bg-info-soft text-info' },
  ran: { text: 'Ran', tone: 'border-success/40 bg-success-soft text-success' },
  ran_late: { text: 'Ran', tone: 'border-success/40 bg-success-soft text-success' },
  woken: { text: 'Ran', tone: 'border-success/40 bg-success-soft text-success' },
  failed: { text: 'Failed', tone: 'border-danger/40 bg-danger-soft text-danger' },
  did_not_run: { text: 'Did not run', tone: 'border-danger/40 bg-danger-soft text-danger' },
  cancelled: { text: 'Cancelled', tone: 'border-border bg-surface-2 text-text-muted' },
}

export function Pill({ children, tone, className }: { children: React.ReactNode; tone: string; className?: string }) {
  return <span className={cn('inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-medium leading-none', tone, className)}>{children}</span>
}

/** The state pill, plus "7 h late" or "Woken by deadline" where that is what happened. */
export function StateBadges({ row }: { row: FollowupRow }) {
  const kind = kindOf(row)
  const b = BADGE[kind]
  return (
    <span className="inline-flex flex-wrap items-center gap-1" data-testid="followup-state" data-kind={kind}>
      <Pill tone={b.tone}>{b.text}</Pill>
      {kind === 'ran_late' && <Pill tone="border-warning/40 bg-warning-soft text-warning">{Math.round((row.late_by ?? 0) / 3600)} h late</Pill>}
      {kind === 'woken' && <Pill tone="border-warning/40 bg-warning-soft text-warning">Woken by deadline</Pill>}
    </span>
  )
}

// ---- actions

/** Cancel the follow-up: one signed click on a real host; on the mock it happens at once and an Undo toast follows. */
export function CancelButton({ row, ws, canUndo, size = 'sm' }: { row: FollowupRow; ws: string; canUndo: boolean; size?: 'sm' | 'xs' }) {
  const refresh = useRefresh(ws)
  const [signing, setSigning] = useState(false)
  const [busy, setBusy] = useState(false)
  if (!row.can_cancel || !row.ticket) return null
  const done = () => {
    refresh()
  }
  const label = 'Cancel follow-up'
  const mock = async () => {
    setBusy(true)
    try {
      await api.runAddonAction(ws, 'schedules', 'fu_cancel', { id: row.id })
      done()
      toast(`Cancelled the follow-up on ${row.ticket}.`, {
        duration: 15_000,
        ...(canUndo
          ? {
              action: {
                label: 'Undo',
                onClick: () =>
                  void api.runAddonAction(ws, 'schedules', 'fu_undo', { id: row.id }).then(done, (e) => toastApiError(e, 'Could not undo the cancel.')),
              },
            }
          : {}),
      })
    } catch (e) {
      toastApiError(e, 'Could not cancel the follow-up.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <Button
        type="button"
        size={size}
        variant="outline"
        disabled={busy}
        className="border-danger/50 text-danger hover:bg-danger-soft hover:text-danger"
        onClick={() => (isRealHost() ? setSigning(true) : void mock())}
      >
        {label}
      </Button>
      {signing && (
        <SignPopover
          title={`Cancel the follow-up on ${row.ticket}`}
          ws={ws}
          action={{ action: 'followup_cancel', key: row.ticket, id: row.id }}
          autoStart
          autoSign
          confirmLabel={label}
          doneToast={`Cancelled the follow-up on ${row.ticket}.`}
          onSettled={done}
          onClose={() => setSigning(false)}
        />
      )}
    </>
  )
}

/**
 * Run now as me: starts the agent on this ticket right away with the clicker's own permissions (the label says it; no extra
 * dialog). On a real host that is the ordinary Start agent dialog, which shows what runs; starting an agent by hand also
 * ends the wait, so the follow-up goes by itself.
 */
export function RunNowButton({ row, ws, primary = false, size = 'sm' }: { row: FollowupRow; ws: string; primary?: boolean; size?: 'sm' | 'xs' }) {
  const refresh = useRefresh(ws)
  const [starting, setStarting] = useState(false)
  const [busy, setBusy] = useState(false)
  const real = isRealHost()
  if (!row.ticket || (real && !hasFeature('agent_start', ws))) return null
  const click = async () => {
    if (real) return setStarting(true)
    setBusy(true)
    try {
      const r = await api.runAddonAction(ws, 'schedules', 'fu_run_now', { id: row.id })
      toast.success(r.message ?? 'Started. It runs as you.')
      refresh()
    } catch (e) {
      toastApiError(e, 'Could not start the agent.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <Button
        type="button"
        size={size}
        variant={primary ? 'default' : 'outline'}
        disabled={busy}
        title="Starts the agent on this ticket now, with your own permissions."
        onClick={() => void click()}
      >
        Run now as me
      </Button>
      {real && starting && (
        <StartAgentDialog ws={ws} ticket={row.ticket} open choose initial={{ mode: 'continue', note: 'Starting now replaces the wait. The agent runs as you.' }} onClose={() => setStarting(false)} onStarted={() => { setStarting(false); refresh() }} />
      )}
    </>
  )
}

const localInput = (d: Date) => {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

/** Reschedule without typing a time: three quick times, or a picked date and time. Not drawn where the host cannot move one. */
export function RescheduleButton({ row, ws, canMove, size = 'sm' }: { row: FollowupRow; ws: string; canMove: boolean; size?: 'sm' | 'xs' }) {
  const refresh = useRefresh(ws)
  const [open, setOpen] = useState(false)
  const [custom, setCustom] = useState(false)
  const [value, setValue] = useState(() => localInput(new Date(nowMs() + 24 * 3_600_000)))
  const [busy, setBusy] = useState(false)
  if (!canMove || !row.ticket) return null
  const move = async (at: Date) => {
    setBusy(true)
    try {
      await api.runAddonAction(ws, 'schedules', 'fu_reschedule', { id: row.id, at: at.toISOString() })
      toast.success(`Moved the follow-up on ${row.ticket} to ${fmtExact(at.toISOString())}.`)
      setOpen(false)
      refresh()
    } catch (e) {
      toastApiError(e, 'Could not move the follow-up.')
    } finally {
      setBusy(false)
    }
  }
  const picked = new Date(value)
  const valid = !Number.isNaN(picked.getTime()) && picked.getTime() >= nowMs() + 3_600_000
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" size={size} variant="outline" aria-haspopup="dialog">
          Reschedule
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 space-y-2 p-3" aria-label="Run it again at">
        <p className="text-[12px] text-text-muted">Run it again at</p>
        <div className="flex flex-wrap gap-1.5">
          {quickPicks().map((p) => (
            <Button key={p.label} type="button" size="sm" variant="outline" disabled={busy} onClick={() => void move(p.at)}>
              {p.label}
            </Button>
          ))}
          <Button type="button" size="sm" variant="outline" aria-expanded={custom} onClick={() => setCustom((c) => !c)}>
            Pick a date and time
          </Button>
        </div>
        {custom && (
          <div className="space-y-1.5">
            <input
              type="datetime-local"
              aria-label="Date and time"
              value={value}
              min={localInput(new Date(nowMs() + 3_600_000))}
              onChange={(e) => setValue(e.target.value)}
              className="h-8 w-full rounded-md border border-border bg-bg px-2 text-[13px]"
            />
            <Button type="button" size="sm" disabled={busy || !valid} onClick={() => void move(picked)}>
              Move it
            </Button>
            {!valid && <p className="text-[12px] text-text-faint">Pick a time at least an hour from now.</p>}
          </div>
        )}
        {row.trigger !== 'time' && <p className="text-[12px] text-text-faint">It still wakes earlier when the job finishes.</p>}
      </PopoverContent>
    </Popover>
  )
}
