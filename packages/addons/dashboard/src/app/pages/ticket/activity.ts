import type { TicketDocument } from '@/api/types'
import { fmtAge } from '@/lib/time'
import type { ProgressRun } from './progress'

const short = (s: string, n = 60) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

/**
 * What the agent is doing right now, in plain words, from data the ticket page already holds (P15): the steps, the task
 * leases, an open question and the run. Null when nothing can be said. Times count up with `now`.
 */
export function activityLine(ticket: TicketDocument, steps: Record<'understand' | 'build' | 'check' | 'done', 'done' | 'current' | 'todo'>, run: ProgressRun | null | undefined, now: number): string | null {
  if (ticket.questions_state.some((q) => q.state === 'open')) return 'Waiting for your answer'
  if (steps.understand === 'current') return ticket.gates.requirements.state === 'approved' ? 'Writing the plan' : 'Writing the requirements'
  if (steps.check === 'current' && steps.build === 'done') return 'Checking the result'
  if (steps.build === 'current') {
    const task = ticket.tasks_state.find((t) => t.state === 'doing' && t.lease)
    if (task?.lease) return `Building: ${short(task.text)} · ${fmtAge(task.lease.since, now)}`
    return run?.started_at ? `Working on it · ${fmtAge(run.started_at, now)}` : 'Working on it'
  }
  return null
}
