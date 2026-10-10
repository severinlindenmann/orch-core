import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '@/api/client'
import { APPROVER_GROUPS, appliesText, approversText, policySentence, unmeetablePolicy as unmeetable } from '@/api/gates'
import type { CodeReviewApplies, GateName, TicketType, Workspace } from '@/api/types'
import { Section } from '../ticket/shared'
import { cn } from '@/lib/utils'
import { OwnerNote } from './OwnerNote'
import { useSettingsSign } from './useSettingsSign'

type Policy = Workspace['gates'][GateName]

const TYPES: TicketType[] = ['feature', 'bug', 'chore', 'spike', 'epic']

const GATES: { id: GateName; label: string }[] = [
  { id: 'requirements', label: 'Requirements' },
  { id: 'plan', label: 'Plan' },
  { id: 'verify', label: 'Verify' },
]

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
        <p className="mt-1 text-[13px] text-text-muted">Approvals already given stay valid; new approvals use the new policy. To re-review an approved ticket, request changes on it.</p>
      </div>
      {GATES.map(({ id, label }) => {
        const p = workspace.gates[id]
        const waiting = tickets.data?.filter((t) => t.awaiting_gate === id).length
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
      <CodeReview workspace={workspace} canEdit={canEdit} ask={ask} />
      {prompt}
    </div>
  )
}

/**
 * The opt-in code review gate (owner decision 2026-10-10): off by default; on for every ticket or for ticket types.
 * When on it follows a pass verdict, signs exactly the commit the verdict signed, is never given by an assignee and
 * is never auto-approved (a factory charter included). Landing needs it.
 */
function CodeReview({ workspace, canEdit, ask }: { workspace: Workspace; canEdit: boolean; ask: ReturnType<typeof useSettingsSign>['ask'] }) {
  const p = workspace.gates.code
  const applies: CodeReviewApplies = p.applies ?? 'off'
  const mode = applies === 'off' || applies === 'all' ? applies : 'types'
  const [types, setTypes] = useState<TicketType[]>(Array.isArray(applies) ? applies : ['feature', 'bug', 'chore'])
  const [refused, setRefused] = useState<string | null>(null)
  const save = (next: { approvers: string; count: number; applies: CodeReviewApplies }) => {
    const why = unmeetable(workspace, next)
    if (why) return setRefused(why)
    setRefused(null)
    ask({
      title: 'Change the code review gate',
      covers: [
        'Gate: Code review',
        `Where: ${appliesText(next.applies)}`,
        `Approvers: ${approversText(next.approvers)}`,
        `Approvals needed: ${next.count}`,
        'Never an assignee of the ticket; never auto-approved, not even under a factory charter',
        'It signs the commit the verdict signed; landing needs it',
        'Approvals already given stay valid',
      ],
      req: { op: 'gate.policy', gate: 'code', approvers: next.approvers, count: next.count, not: 'assignees', applies: next.applies },
    })
  }
  const base = { approvers: p.approvers, count: p.count, applies }
  return (
    <Section title="Code review">
      <p className="text-[13px]">
        {appliesText(applies)}.{' '}
        {applies === 'off' ? 'Off by default: the verdict alone lets a ticket land.' : `${policySentence('Code review', { ...p, not: 'assignees' })} It follows a pass verdict and signs the same commit; landing needs it.`}
      </p>
      <fieldset className="mt-3 space-y-2" disabled={!canEdit}>
        <legend className="sr-only">Where the code review applies</legend>
        {(
          [
            ['off', 'Off'],
            ['all', 'Every ticket'],
            ['types', 'Only these ticket types'],
          ] as const
        ).map(([v, label]) => (
          <label key={v} className="flex items-center gap-2 text-[13px]">
            <input
              type="radio"
              name="code-review-applies"
              value={v}
              checked={mode === v}
              onChange={() => mode !== v && save({ ...base, applies: v === 'types' ? types : v })}
              className="accent-[var(--brand)]"
            />
            {label}
          </label>
        ))}
        <div role="group" aria-label="Ticket types that need a code review" className="ml-6 flex flex-wrap gap-3">
          {TYPES.map((t) => (
            <label key={t} className="flex items-center gap-1.5 text-[13px]">
              <input
                type="checkbox"
                checked={types.includes(t)}
                disabled={!canEdit}
                onChange={(e) => {
                  const next = e.target.checked ? [...types, t] : types.filter((x) => x !== t)
                  setTypes(next)
                  if (mode === 'types' && next.length) save({ ...base, applies: TYPES.filter((x) => next.includes(x)) })
                }}
                className="accent-[var(--brand)]"
              />
              {t}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="mt-3 flex flex-wrap items-center gap-4">
        <div role="group" aria-label="Code review approvals" className="inline-flex overflow-hidden rounded-md border border-border">
          {[1, 2, 3].map((n) => (
            <button
              key={n}
              type="button"
              aria-label={`Code review: ${n} approval${n === 1 ? '' : 's'}`}
              aria-pressed={p.count === n}
              disabled={!canEdit}
              onClick={() => p.count !== n && save({ ...base, count: n })}
              className={cn('h-8 w-9 text-[13px] tabular-nums disabled:cursor-not-allowed disabled:opacity-60', p.count === n ? 'bg-brand-soft text-brand' : 'text-text-muted hover:bg-surface-2')}
            >
              {n}
            </button>
          ))}
        </div>
        <select
          aria-label="Code review approvers"
          value={p.approvers}
          disabled={!canEdit}
          onChange={(e) => save({ ...base, approvers: e.target.value })}
          className="h-8 rounded-md border border-border bg-bg px-2 text-[13px] disabled:cursor-not-allowed disabled:opacity-60"
        >
          {APPROVER_GROUPS.map((a) => (
            <option key={a.value} value={a.value}>
              {a.label}
            </option>
          ))}
        </select>
        <span className="text-[13px] text-text-muted">Never the assignees</span>
      </div>
      {refused && (
        <p role="alert" className="mt-3 text-[13px] text-danger">
          {refused}
        </p>
      )}
      <div data-testid="factory-code-review" className="mt-3 rounded-md border border-border bg-bg px-3 py-2 text-[12px] text-text-muted">
        <p className="font-medium text-text">Factory tickets</p>
        <p className="mt-0.5">
          The AI Factory&apos;s charter approves everything on the children of its epic, verdicts included: no person reviews that work. To have a person review the code of factory tickets, turn the code review on
          for every ticket, or for the types the factory makes (feature, bug, chore). A charter never approves a code review, so those children wait in testing for a person before they land.
        </p>
      </div>
      {!canEdit && (
        <p className="mt-3">
          <OwnerNote>Only owners can change this gate.</OwnerNote>
        </p>
      )}
    </Section>
  )
}
