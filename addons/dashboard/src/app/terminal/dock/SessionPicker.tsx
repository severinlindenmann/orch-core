// "Sessions for DEMO-0043": running sessions to join (your own shell, interactive) or watch (agents, view only), a new
// session (harness, with the ticket's context or a fresh window), and earlier sessions (read the transcript, or resume:
// a new session seeded with the old summary).

import { useId, useState } from 'react'
import { HARNESSES, harnessCommand, harnessOf, type HarnessId } from '@/api/harnesses'
import type { TerminalSessionView } from '@/api/terminals'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const hhmm = (iso: string) => `${iso.slice(11, 16)} UTC`
const when = (iso: string, now?: string) => (now && iso.slice(0, 10) === now.slice(0, 10) ? `today ${hhmm(iso)}` : `${iso.slice(5, 10)} ${hhmm(iso)}`)

/** Who a session belongs to, in a few words. */
function whose(s: TerminalSessionView, me: string) {
  if (s.kind === 'agent') return `${harnessOf(s.harness).label} agent · view only`
  return s.owner === me ? (s.interactive ? 'Yours · interactive' : 'Yours · view only') : 'View only'
}

export function SessionPicker({ ticket, running, ended, current, me, now, canStart, onSelect, onStart, onResume, className }: {
  ticket?: string
  running: TerminalSessionView[]
  ended: TerminalSessionView[]
  current: string | null
  me: string
  now?: string
  canStart: boolean
  onSelect: (id: string) => void
  onStart: (harness: HarnessId, context: boolean) => void
  onResume: (id: string) => void
  className?: string
}) {
  const [harness, setHarness] = useState<HarnessId>('claude')
  const [context, setContext] = useState(true)
  const headId = useId()
  const withContext = !!ticket && context && !!harnessOf(harness).withContext
  return (
    <section aria-labelledby={headId} className={cn('min-h-0 overflow-y-auto bg-surface px-3 py-2 text-xs', className)}>
      <h3 id={headId} className="mb-2 text-[13px] font-semibold">{ticket ? `Sessions for ${ticket}` : 'Sessions in this workspace'}</h3>

      <h4 className="mb-1 text-[11px] font-medium uppercase tracking-wide text-text-faint">Running</h4>
      {running.length === 0 ? (
        <p className="mb-3 text-text-muted">Nothing is running{ticket ? ` for ${ticket}` : ''}.</p>
      ) : (
        <ul className="mb-3 space-y-1">
          {running.map((s) => {
            const join = s.interactive
            return (
              <li key={s.id} className={cn('flex items-center gap-2 rounded px-2 py-1', s.id === current && 'bg-surface-2')}>
                <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-success" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12px]">{s.label}</span>
                  <span className="block truncate text-text-muted">{whose(s, me)} · started {when(s.started, now)}</span>
                </span>
                <Button variant={join ? 'secondary' : 'ghost'} size="xs" aria-label={`${join ? 'Join' : 'Watch'} ${s.label}${join ? '' : ' (view only)'}`} onClick={() => onSelect(s.id)}>
                  {join ? 'Join' : 'Watch'}
                </Button>
              </li>
            )
          })}
        </ul>
      )}

      <h4 className="mb-1 text-[11px] font-medium uppercase tracking-wide text-text-faint">New session</h4>
      <form
        className="mb-3 space-y-2 rounded border border-border p-2"
        onSubmit={(e) => {
          e.preventDefault()
          onStart(harness, withContext)
        }}
      >
        <fieldset className="flex flex-wrap items-center gap-x-3 gap-y-1" disabled={!canStart}>
          <legend className="sr-only">Harness</legend>
          {HARNESSES.map((h) => (
            <label key={h.id} className="flex items-center gap-1">
              <input type="radio" name={`${headId}-harness`} value={h.id} checked={harness === h.id} onChange={() => setHarness(h.id)} className="accent-brand" />
              {h.label}
            </label>
          ))}
        </fieldset>
        {ticket && harnessOf(harness).withContext && (
          <fieldset className="flex flex-wrap items-center gap-x-3 gap-y-1" disabled={!canStart}>
            <legend className="sr-only">Context</legend>
            <label className="flex items-center gap-1">
              <input type="radio" name={`${headId}-context`} checked={context} onChange={() => setContext(true)} className="accent-brand" />
              With {ticket} context
            </label>
            <label className="flex items-center gap-1">
              <input type="radio" name={`${headId}-context`} checked={!context} onChange={() => setContext(false)} className="accent-brand" />
              Without context (fresh window)
            </label>
          </fieldset>
        )}
        <code className="block break-all rounded bg-bg px-1.5 py-1 font-mono text-[11px] text-text-muted" aria-label="Command">
          {harnessCommand(harness, { ticket, context: withContext })}
        </code>
        <div className="flex items-center gap-2">
          <Button type="submit" size="xs" disabled={!canStart}>
            Start {harnessOf(harness).label}
          </Button>
          <span className="text-text-faint">{canStart ? `Your own session${ticket ? ` in the ${ticket} worktree` : ''}.` : 'Viewers can watch agent sessions but cannot start one.'}</span>
        </div>
      </form>

      <h4 className="mb-1 text-[11px] font-medium uppercase tracking-wide text-text-faint">Earlier sessions</h4>
      {ended.length === 0 ? (
        <p className="text-text-muted">No earlier sessions{ticket ? ` for ${ticket}` : ''}.</p>
      ) : (
        <ul className="space-y-1">
          {ended.map((s) => (
            <li key={s.id} className={cn('rounded px-2 py-1', s.id === current && 'bg-surface-2')}>
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12px]">{s.label}</span>
                  <span className="block truncate text-text-muted">Ended · started {when(s.started, now)}</span>
                </span>
                <Button variant="ghost" size="xs" aria-label={`Transcript of ${s.label}`} onClick={() => onSelect(s.id)}>
                  Transcript
                </Button>
                <Button variant="secondary" size="xs" aria-label={`Resume ${s.label}`} disabled={!canStart} onClick={() => onResume(s.id)}>
                  Resume
                </Button>
              </div>
              <p className="mt-0.5 line-clamp-2 text-text-muted">{s.summary ?? 'No summary recorded.'}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
