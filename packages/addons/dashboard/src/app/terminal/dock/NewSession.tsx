// "New session": a compact form in a popover — harness (a select; the last one used is remembered), where it runs
// (the ticket's worktree or the workspace), whether to include the ticket's current-state summary, the exact command
// behind "Show command", Start.

import { Plus } from 'lucide-react'
import { useId, useState } from 'react'
import { findHarness, harnessCommand, HARNESSES } from '@/api/harnesses'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

export interface NewSessionChoice {
  harness: string
  /** Run in the ticket's worktree (else the workspace). */
  inTicket: boolean
  /** Load the ticket's current-state summary (harnesses with contextInjection only). */
  summary: boolean
}

const STARTABLE = HARNESSES.filter((h) => h.capabilities.interactive)

export function NewSessionForm({ ticket, lastHarness, canStart, onStart }: {
  ticket?: string
  lastHarness: string
  canStart: boolean
  onStart: (choice: NewSessionChoice) => void
}) {
  const id = useId()
  const [harness, setHarness] = useState(() => (STARTABLE.some((h) => h.id === lastHarness) ? lastHarness : STARTABLE[0].id))
  const [inTicket, setInTicket] = useState(!!ticket)
  const [summary, setSummary] = useState(true)
  const [showCommand, setShowCommand] = useState(false)
  const h = findHarness(harness)!
  const where = !!ticket && inTicket
  const withSummary = where && summary && h.capabilities.contextInjection
  return (
    <form
      aria-label="New session"
      className="space-y-2 text-xs"
      onSubmit={(e) => {
        e.preventDefault()
        onStart({ harness, inTicket: where, summary: withSummary })
      }}
    >
      <fieldset disabled={!canStart} className="space-y-2">
        <label className="flex items-center gap-2">
          <span className="w-12 text-text-muted">Harness</span>
          <select name={`${id}-harness`} id={`${id}-harness`} value={harness} onChange={(e) => setHarness(e.target.value)} className="h-7 flex-1 rounded-md border border-border bg-bg px-2 text-xs">
            {STARTABLE.map((x) => (
              <option key={x.id} value={x.id}>{x.label}</option>
            ))}
          </select>
        </label>
        {ticket && (
          <div role="radiogroup" aria-label="Where" className="flex items-center gap-2">
            <span className="w-12 text-text-muted">Where</span>
            <label className="flex items-center gap-1">
              <input type="radio" id={`${id}-where-ticket`} name={`${id}-where`} checked={inTicket} onChange={() => setInTicket(true)} className="accent-brand" />
              Ticket worktree
            </label>
            <label className="flex items-center gap-1">
              <input type="radio" id={`${id}-where-workspace`} name={`${id}-where`} checked={!inTicket} onChange={() => setInTicket(false)} className="accent-brand" />
              Workspace
            </label>
          </div>
        )}
        {where && h.capabilities.contextInjection && (
          <label className="flex items-center gap-2">
            <input type="checkbox" name={`${id}-summary`} id={`${id}-summary`} checked={summary} onChange={(e) => setSummary(e.target.checked)} className="accent-brand" />
            Include the ticket's current-state summary
          </label>
        )}
      </fieldset>
      <div className="flex items-center gap-2">
        <Button type="submit" size="xs" disabled={!canStart}>Start {h.label}</Button>
        <Button type="button" variant="ghost" size="xs" aria-expanded={showCommand} onClick={() => setShowCommand(!showCommand)}>
          {showCommand ? 'Hide command' : 'Show command'}
        </Button>
      </div>
      {showCommand && (
        <code aria-label="Command" className="block break-all rounded bg-bg px-1.5 py-1 font-mono text-[11px] text-text-muted">
          {harnessCommand(harness, { ticket: where ? ticket : null, context: withSummary })}
        </code>
      )}
      <p className="text-text-faint">{canStart ? `Your own session${where ? ` in the ${ticket} worktree` : ' in the workspace'}. Simulated: nothing runs.` : 'Viewers can watch agent sessions but cannot start one.'}</p>
    </form>
  )
}

export function NewSessionButton({ open, onOpenChange, ...form }: Parameters<typeof NewSessionForm>[0] & { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button variant="secondary" size="xs" aria-label="New session" title="New session"><Plus /><span className="hidden @[26rem]/dock:inline">New session</span></Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-3">
        <NewSessionForm {...form} onStart={(c) => { onOpenChange(false); form.onStart(c) }} />
      </PopoverContent>
    </Popover>
  )
}
