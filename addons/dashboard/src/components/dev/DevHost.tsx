// Core D1 (#582): the dev sandbox host, made impossible to mistake for a normal one. The badge is the same message as the
// phone's red banner; it has no close button and is drawn on every page (the app shell). The alarms list is for the host's
// General page. Nothing here is drawn on a normal host: `hostInfoOf` says nothing and the old pages stay as they were.
import { useQuery } from '@tanstack/react-query'
import { TriangleAlert } from 'lucide-react'
import { DEV_CONVENIENCE_REFUSED, SANDBOX_REGISTERED, type HostAlarm } from '@/api/hostInfo'
import { queries } from '@/api/queries'

export const DEV_BADGE_LABEL = 'Dev sandbox host: Simulator convenience answers are accepted here. This is not a normal host.'

export function DevBadge() {
  const info = useQuery(queries.hostInfo())
  const dev = info.data?.dev
  // A safety cue must not fail open: when the read fails, a dev host would look like a normal one. Say so (it is retried).
  if (info.isError && !info.data)
    return (
      <span
        role="status"
        aria-label="Host status unavailable: this host may be a dev sandbox host."
        data-testid="dev-status-unavailable"
        className="inline-flex max-w-full shrink-0 items-center gap-1.5 rounded-md border border-border bg-surface-3 px-2 py-1 text-[11px] font-medium text-text-muted"
      >
        <TriangleAlert className="size-3.5 shrink-0" aria-hidden />
        <span>Host status unavailable</span>
      </span>
    )
  if (!dev) return null
  return (
    <span
      role="status"
      aria-label={dev.sandbox ? `${DEV_BADGE_LABEL} Sandbox ${dev.sandbox}.` : DEV_BADGE_LABEL}
      data-testid="dev-badge"
      className="inline-flex max-w-full shrink-0 items-center gap-1.5 rounded-md bg-[#b91c1c] px-2 py-1 text-[11px] font-bold uppercase tracking-wide text-white"
    >
      <TriangleAlert className="size-3.5 shrink-0" aria-hidden />
      <span>DEV SANDBOX</span>
      {dev.sandbox && (
        <span data-testid="dev-badge-sandbox" className="font-mono font-normal normal-case opacity-90">
          {dev.sandbox}
        </span>
      )}
    </span>
  )
}

/** One alarm as a sentence. An unknown code is "unknown alarm" and the code, drawn only as a code. */
export function alarmText(a: HostAlarm): { text: string; code: string | null } {
  if (a.code === SANDBOX_REGISTERED)
    return { text: 'This normal host skipped a throwaway dev sandbox workspace at start, so it is not served here.', code: null }
  if (a.code === DEV_CONVENIENCE_REFUSED)
    return { text: 'A dev convenience answer was refused.', code: null }
  return { text: 'unknown alarm', code: a.code }
}

export function HostAlarms({ alarms }: { alarms: HostAlarm[] }) {
  if (alarms.length === 0) return null
  return (
    <section aria-label="Host alarms" data-testid="host-alarms" className="space-y-2 rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-[13px]">
      <h3 className="font-semibold text-danger">Host alarms</h3>
      <ul className="space-y-1">
        {alarms.map((a, i) => {
          const { text, code } = alarmText(a)
          return (
            <li key={i} data-testid="host-alarm" className="min-w-0 break-words">
              {text}
              {code && (
                <>
                  {' '}
                  <code className="break-all rounded bg-surface-3 px-1 font-mono text-[12px] text-text">{code}</code>
                </>
              )}
              {a.workspace_id && (
                <>
                  {' '}
                  Workspace <code className="break-all rounded bg-surface-3 px-1 font-mono text-[12px] text-text">{a.workspace_id}</code>.
                </>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}

/** L13: what lets an agent user reach your files, from the host's isolation check. Same look as the host alarms; drawn only when there is something. */
export function IsolationProblems({ problems }: { problems: { what: string; fix: string }[] }) {
  if (problems.length === 0) return null
  return (
    <section aria-label="Isolation problems" data-testid="isolation-problems" className="space-y-2 rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-[13px]">
      <h3 className="font-semibold text-danger">Agents could reach your files</h3>
      <ul className="space-y-1">
        {problems.map((p, i) => (
          <li key={i} data-testid="isolation-problem" className="min-w-0 break-words">
            {p.what}. <span className="text-text-muted">To fix: {p.fix}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}
