// "Ticket info": facts other addons already hold (agent cost from usage, PR checks from github), read from their
// states only while they are active, shown in a popover instead of chips in the toolbar.

import { Info } from 'lucide-react'
import { addonActive } from '@/api/addons'
import { useAddonStates } from '@/addon-ui/slots'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useWorkspace } from '../../workspace'
import { plural } from '@/lib/time'

const chf = (cents: number) => `CHF ${(cents / 100).toFixed(2)}`

interface UsageState { byTicket?: Record<string, { cents: number; sessions: number }>; perDay?: { chf: number }[] }
interface GithubState { prByTicket?: Record<string, { number: number; checks: string }>; checksFailing?: number }

export function useInfoRows(ticket?: string): { label: string; value: string; from: string }[] {
  const { workspace } = useWorkspace()
  const names = ['usage', 'github'].filter((n) => addonActive(workspace, n))
  const states = useAddonStates(workspace?.id, names)
  const usage = states.usage as UsageState | undefined
  const github = states.github as GithubState | undefined
  const rows: { label: string; value: string; from: string }[] = []
  if (usage) {
    const u = ticket ? usage.byTicket?.[ticket] : undefined
    const today = usage.perDay?.at(-1)
    if (u) rows.push({ label: 'Agent cost', value: `${chf(u.cents)} · ${plural(u.sessions, 'session')}`, from: 'Usage' })
    else if (!ticket && today) rows.push({ label: 'Agent cost today', value: chf(Math.round(today.chf * 100)), from: 'Usage' })
  }
  if (github) {
    const pr = ticket ? github.prByTicket?.[ticket] : undefined
    if (pr) rows.push({ label: 'Pull request', value: `#${pr.number} · checks ${pr.checks}`, from: 'GitHub' })
    else if (!ticket && github.checksFailing) rows.push({ label: 'Failing checks', value: `${github.checksFailing} pull requests`, from: 'GitHub' })
  }
  return rows
}

export function TicketInfo({ ticket, compact = false }: { ticket?: string; compact?: boolean }) {
  const rows = useInfoRows(ticket)
  if (rows.length === 0) return null
  const title = ticket ? 'Ticket info' : 'Workspace info'
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size={compact ? 'icon-xs' : 'xs'} aria-label={title} title={title}><Info />{!compact && title}</Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-64 p-3 text-xs">
        <dl aria-label={title} className="space-y-1.5">
          {rows.map((r) => (
            <div key={r.label} className="flex items-baseline gap-2">
              <dt className="w-24 shrink-0 text-text-muted">{r.label}</dt>
              <dd className="tabular-nums">{r.value} <span className="text-text-faint">({r.from})</span></dd>
            </div>
          ))}
        </dl>
      </PopoverContent>
    </Popover>
  )
}
