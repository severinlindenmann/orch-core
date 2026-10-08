import type { GateName, Workspace } from '@/api/types'
import { Section } from '../ticket/shared'
import { cn } from '@/lib/utils'
import { useSettingsSign } from './useSettingsSign'

type Policy = Workspace['gates'][GateName]

const GATES: { id: GateName; label: string }[] = [
  { id: 'requirements', label: 'Requirements' },
  { id: 'plan', label: 'Plan' },
  { id: 'verify', label: 'Verify' },
]
const APPROVERS = [
  { value: 'owner', who: 'owners', label: 'Owners' },
  { value: 'maintainer', who: 'owners or maintainers', label: 'Owners or maintainers' },
  { value: 'reviewers', who: "the ticket's reviewers", label: "The ticket's reviewers" },
]
const whoOf = (approvers: string) => APPROVERS.find((a) => a.value === approvers)?.who ?? approvers

/** "Plan needs 1 approval from owners or maintainers, not the assignees." */
export function policySentence(label: string, p: Policy): string {
  return `${label} needs ${p.count} approval${p.count === 1 ? '' : 's'} from ${whoOf(p.approvers)}${p.not === 'assignees' ? ', not the assignees' : ''}.`
}

export function Gates({ workspace, canEdit }: { workspace: Workspace; canEdit: boolean }) {
  const { ask, prompt } = useSettingsSign(workspace.id)

  const change = (gate: GateName, label: string, next: Policy) =>
    ask({
      title: `Change the ${label.toLowerCase()} gate`,
      covers: [
        `Gate: ${label}`,
        `Approvers: ${whoOf(next.approvers)}`,
        `Approvals needed: ${next.count}`,
        next.not === 'assignees' ? 'The ticket assignees cannot approve' : 'Assignees may approve',
        'Open approvals stay valid',
      ],
      req: { op: 'gate.policy', gate, approvers: next.approvers, count: next.count, not: next.not === 'assignees' ? 'assignees' : null },
    })

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">Gate policies</h2>
        <p className="mt-1 text-[13px] text-text-muted">Open approvals stay valid; new approvals use the new policy.</p>
      </div>
      {GATES.map(({ id, label }) => {
        const p = workspace.gates[id]
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
                {APPROVERS.map((a) => (
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
          </Section>
        )
      })}
      {prompt}
    </div>
  )
}
