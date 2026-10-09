import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '@/api/client'
import { APPROVER_GROUPS, approversText, policySentence } from '@/api/gates'
import type { GateName, Status, Workspace } from '@/api/types'
import { Section } from '../ticket/shared'
import { cn } from '@/lib/utils'
import { OwnerNote } from './OwnerNote'
import { useSettingsSign } from './useSettingsSign'

type Policy = Workspace['gates'][GateName]

const GATES: { id: GateName; label: string }[] = [
  { id: 'requirements', label: 'Requirements' },
  { id: 'plan', label: 'Plan' },
  { id: 'verify', label: 'Verify' },
]

/** The status a ticket waits in for each gate: backlog for requirements, open for the plan, testing for the verdict. */
const WAITS_IN: Record<GateName, Status> = { requirements: 'backlog', plan: 'open', verify: 'testing' }

/** How many people could ever approve, or null when the policy depends on each ticket (its reviewers). */
function eligibleCount(workspace: Workspace, approvers: string): number | null {
  if (approvers === 'owner') return workspace.members.filter((m) => m.role === 'owner').length
  if (approvers === 'maintainer') return workspace.members.filter((m) => m.role === 'owner' || m.role === 'maintainer').length
  return null
}

/** "Only 1 owner exists; 2 approvals from owners can never be met." or null when the policy can be met. */
function unmeetable(workspace: Workspace, p: Policy): string | null {
  const n = eligibleCount(workspace, p.approvers)
  if (n === null || p.count <= n) return null
  const group = approversText(p.approvers)
  const one = p.approvers === 'owner' ? 'owner' : 'owner or maintainer'
  const many = group
  return `Only ${n} ${n === 1 ? one : many} ${n === 1 ? 'exists' : 'exist'}; ${p.count} approvals from ${group} can never be met.`
}

export function Gates({ workspace, canEdit }: { workspace: Workspace; canEdit: boolean }) {
  const { ask, prompt } = useSettingsSign(workspace.id)
  const [refused, setRefused] = useState<{ gate: GateName; why: string } | null>(null)
  const tickets = useQuery({ queryKey: ['tickets', workspace.id, 'all'], queryFn: () => api.listTickets(workspace.id) })

  const change = (gate: GateName, label: string, next: Policy) => {
    const why = unmeetable(workspace, next)
    if (why) {
      setRefused({ gate, why })
      return
    }
    setRefused(null)
    ask({
      title: `Change the ${label.toLowerCase()} gate`,
      covers: [
        `Gate: ${label}`,
        `Approvers: ${approversText(next.approvers)}`,
        `Approvals needed: ${next.count}`,
        next.not === 'assignees' ? 'The ticket assignees cannot approve' : 'Assignees may approve',
        'Approvals already given stay valid',
      ],
      req: { op: 'gate.policy', gate, approvers: next.approvers, count: next.count, not: next.not === 'assignees' ? 'assignees' : null },
    })
  }

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">Gate policies</h2>
        <p className="mt-1 text-[13px] text-text-muted">Approvals already given stay valid; new approvals use the new policy.</p>
      </div>
      {GATES.map(({ id, label }) => {
        const p = workspace.gates[id]
        const waiting = tickets.data?.filter((t) => t.status === WAITS_IN[id]).length
        return (
          <Section key={id} title={label}>
            <p className="text-[13px]">{policySentence(label, p)}</p>
            <div className="mt-3 flex flex-wrap items-center gap-4">
              <div role="group" aria-label={`${label} approvals`} className="inline-flex overflow-hidden rounded-md border border-border">
                {[1, 2, 3].map((n) => (
                  <button
                    key={n}
                    type="button"
                    aria-label={`${label}: ${n} approval${n === 1 ? '' : 's'}`}
                    aria-pressed={p.count === n}
                    disabled={!canEdit}
                    onClick={() => p.count !== n && change(id, label, { ...p, count: n })}
                    className={cn('h-8 w-9 text-[13px] tabular-nums disabled:cursor-not-allowed disabled:opacity-60', p.count === n ? 'bg-brand-soft text-brand' : 'text-text-muted hover:bg-surface-2')}
                  >
                    {n}
                  </button>
                ))}
              </div>
              <select
                aria-label={`${label} approvers`}
                value={p.approvers}
                disabled={!canEdit}
                onChange={(e) => change(id, label, { ...p, approvers: e.target.value })}
                className="h-8 rounded-md border border-border bg-bg px-2 text-[13px] disabled:cursor-not-allowed disabled:opacity-60"
              >
                {APPROVER_GROUPS.map((a) => (
                  <option key={a.value} value={a.value}>
                    {a.label}
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-2 text-[13px]">
                <input
                  type="checkbox"
                  checked={p.not === 'assignees'}
                  disabled={!canEdit}
                  onChange={(e) => change(id, label, { approvers: p.approvers, count: p.count, ...(e.target.checked ? { not: 'assignees' } : {}) })}
                  className="accent-[var(--brand)]"
                />
                Not the assignees
              </label>
            </div>
            {!canEdit && (
              <p className="mt-3">
                <OwnerNote>Only owners can change this gate.</OwnerNote>
              </p>
            )}
            {refused?.gate === id && (
              <p role="alert" className="mt-3 text-[13px] text-danger">
                {refused.why}
              </p>
            )}
            {waiting !== undefined && (
              <p className="mt-3 text-[12px] text-text-faint">
                {waiting === 0 ? 'No open tickets are waiting at this gate.' : `Affects ${waiting} open ticket${waiting === 1 ? '' : 's'}.`} Approvals already given stay valid.
              </p>
            )}
          </Section>
        )
      })}
      {prompt}
    </div>
  )
}
