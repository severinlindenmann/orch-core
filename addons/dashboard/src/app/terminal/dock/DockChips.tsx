// Compact facts from other addons in the dock header (usage, PR checks). Read from their existing states, only while
// they are active; each chip carries its own addon's A.

import { addonActive } from '@/api/addons'
import { AddonFrame } from '@/addon-ui/AddonFrame'
import { useAddonStates } from '@/addon-ui/slots'
import { useWorkspace } from '../../workspace'

const chf = (cents: number) => `CHF ${(cents / 100).toFixed(2)}`

interface UsageState { byTicket?: Record<string, { cents: number; sessions: number }>; perDay?: { chf: number }[] }
interface GithubState { prByTicket?: Record<string, { number: number; checks: string }>; checksFailing?: number }

/** `row`: on a line of its own under the header (the narrow right-hand dock). */
export function DockChips({ ticket, row = false }: { ticket?: string; row?: boolean }) {
  const { workspace } = useWorkspace()
  const names = ['usage', 'github'].filter((n) => addonActive(workspace, n))
  const states = useAddonStates(workspace?.id, names)
  const usage = states.usage as UsageState | undefined
  const github = states.github as GithubState | undefined
  const chips: { addon: string; title: string; text: string; hint: string }[] = []
  if (usage) {
    if (ticket) {
      const u = usage.byTicket?.[ticket]
      if (u) chips.push({ addon: 'usage', title: 'Usage', text: `${chf(u.cents)} · ${u.sessions} sessions`, hint: `Agent cost of ${ticket} so far` })
    } else {
      const today = usage.perDay?.at(-1)
      if (today) chips.push({ addon: 'usage', title: 'Usage', text: `Today ${chf(Math.round(today.chf * 100))}`, hint: 'Agent cost in this workspace today' })
    }
  }
  if (github) {
    if (ticket) {
      const pr = github.prByTicket?.[ticket]
      if (pr) chips.push({ addon: 'github', title: 'GitHub', text: `#${pr.number} checks ${pr.checks}`, hint: `Pull request for ${ticket}` })
    } else if (github.checksFailing) chips.push({ addon: 'github', title: 'GitHub', text: `${github.checksFailing} failing checks`, hint: 'Open pull requests with failing checks' })
  }
  if (chips.length === 0) return null
  return (
    <ul aria-label="Addon info" className={row ? 'flex h-7 shrink-0 items-center gap-1.5 overflow-hidden border-b border-border bg-surface px-2' : 'flex min-w-0 shrink items-center gap-1.5 overflow-hidden'}>
      {chips.map((c) => (
        <li key={c.addon} className="shrink-0" title={c.hint}>
          <AddonFrame compact addon={c.addon} addonTitle={c.title} title={c.hint} className="py-0 text-[11px] text-text-muted">
            <span className="tabular-nums">{c.text}</span>
          </AddonFrame>
        </li>
      ))}
    </ul>
  )
}
