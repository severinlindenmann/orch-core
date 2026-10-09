// Sessions as a browser: "Running (n)" to join (your own, you can type) or watch (agents, read only), and
// "Earlier (n)" to view a transcript or continue from its summary (a new session seeded with it).

import { useId } from 'react'
import { findHarness } from '@/api/harnesses'
import type { TerminalSessionView } from '@/api/terminals'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

const hhmm = (iso: string) => `${iso.slice(11, 16)} UTC`
const when = (iso: string, now?: string) => (now && iso.slice(0, 10) === now.slice(0, 10) ? `today ${hhmm(iso)}` : `${iso.slice(5, 10)} ${hhmm(iso)}`)

function whose(s: TerminalSessionView) {
  if (s.kind === 'agent') return 'Agent · read only'
  return s.interactive ? 'Yours · you can type' : 'Yours · read only'
}

export function SessionBrowser({ title, running, ended, names, current, now, canStart, onSelect, onContinue, className }: {
  title: string
  running: TerminalSessionView[]
  ended: TerminalSessionView[]
  names: (s: TerminalSessionView) => string
  current: string | null
  now?: string
  canStart: boolean
  onSelect: (id: string) => void
  onContinue: (id: string) => void
  className?: string
}) {
  const head = useId()
  return (
    <section aria-labelledby={head} className={cn('flex min-h-0 flex-col bg-surface px-3 py-2 text-xs', className)}>
      <h3 id={head} className="mb-1 text-[13px] font-semibold">{title}</h3>
      <Tabs defaultValue={running.length || !ended.length ? 'running' : 'earlier'} className="min-h-0 flex-1 gap-1">
        <TabsList variant="line" className="h-7 w-full justify-start border-b border-border">
          <TabsTrigger value="running" className="flex-none text-xs">Running ({running.length})</TabsTrigger>
          <TabsTrigger value="earlier" className="flex-none text-xs">Earlier ({ended.length})</TabsTrigger>
        </TabsList>
        <TabsContent value="running" className="min-h-0 overflow-y-auto">
          {running.length === 0 ? (
            <p className="py-2 text-text-muted">Nothing is running here. Start one with New session.</p>
          ) : (
            <ul className="space-y-0.5">
              {running.map((s) => {
                const join = s.interactive
                return (
                  <li key={s.id} className={cn('flex items-center gap-2 rounded px-2 py-1', s.id === current && 'bg-surface-2')}>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-[12px]">{names(s)}</span>
                      <span className="block truncate text-text-muted">{whose(s)} · started {when(s.started, now)}</span>
                    </span>
                    <Button variant={join ? 'secondary' : 'ghost'} size="xs" aria-label={`${join ? 'Join' : 'Watch'} ${names(s)}${join ? '' : ' (read only)'}`} onClick={() => onSelect(s.id)}>
                      {join ? 'Join' : 'Watch'}
                    </Button>
                  </li>
                )
              })}
            </ul>
          )}
        </TabsContent>
        <TabsContent value="earlier" className="min-h-0 overflow-y-auto">
          {ended.length === 0 ? (
            <p className="py-2 text-text-muted">No earlier sessions here.</p>
          ) : (
            <ul className="space-y-1">
              {ended.map((s) => {
                const h = findHarness(s.harness)
                const canContinue = !!h?.capabilities.interactive
                return (
                  <li key={s.id} className={cn('rounded px-2 py-1', s.id === current && 'bg-surface-2')}>
                    <div className="flex items-center gap-2">
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[12px]">{names(s)}</span>
                        <span className="block truncate text-text-muted">Ended · started {when(s.started, now)}</span>
                      </span>
                      <Button variant="ghost" size="xs" aria-label={`View transcript of ${names(s)}`} onClick={() => onSelect(s.id)}>
                        View transcript
                      </Button>
                    </div>
                    <p className="mt-0.5 line-clamp-2 text-text-muted">{s.summary ?? 'No summary recorded.'}</p>
                    {canContinue ? (
                      <div className="mt-1 flex items-center gap-2">
                        <Button variant="secondary" size="xs" aria-label={`Continue from summary of ${names(s)}`} disabled={!canStart} onClick={() => onContinue(s.id)}>
                          Continue from summary
                        </Button>
                        <span className="text-text-faint">Starts a new {h.label} session using this summary.</span>
                      </div>
                    ) : (
                      <p className="mt-1 text-text-faint">Unsupported harness {s.harness}: transcript only.</p>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </TabsContent>
      </Tabs>
    </section>
  )
}
