import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { APPROVER_TOKENS, GATE_LABEL, POLICY_GATES, TICKET_TYPES, TOKEN_LABEL, canChangeSettings, canonicalPolicy, policyProblem, samePolicy, type HostPolicy, type PolicyGate } from '@/api/settings'
import { hasFeature } from '@/api/features'
import type { Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { fmtDateTime } from '@/lib/time'
import { queries } from '@/api/queries'
import { Raw } from '@/components/sign/visible'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useViewerRole } from './useViewerRole'
import { useSettingsHandoff } from './useSettingsHandoff'

const sentence = (label: string, p: HostPolicy) => {
  const who = p.approvers.map((a) => TOKEN_LABEL[a as keyof typeof TOKEN_LABEL] ?? a).join(', ')
  const not = p.not.length ? `, but not ${p.not.map((a) => (TOKEN_LABEL[a as keyof typeof TOKEN_LABEL] ?? a).toLowerCase()).join(' or ')}` : ''
  const where = p.applies === 'all' ? 'on every ticket' : p.applies === 'off' ? 'switched off' : `on ${p.applies.join(', ')} tickets`
  return `${label}: ${p.count} approval${p.count === 1 ? '' : 's'} from ${who.toLowerCase()}${not}, ${where}${p.independent ? ', independent' : ''}.`
}

function Toggles({ legend, tokens, chosen, disabled, locked, onChange }: { legend: string; tokens: readonly string[]; chosen: string[]; disabled: boolean; locked?: string[]; onChange: (next: string[]) => void }) {
  return (
    <fieldset className="min-w-0" disabled={disabled}>
      <legend className="mb-1 text-[12px] text-text-muted">{legend}</legend>
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {tokens.map((t) => (
          <label key={t} className="flex items-center gap-1.5 text-[13px]">
            <input
              type="checkbox"
              checked={chosen.includes(t)}
              disabled={locked?.includes(t)}
              onChange={(e) => onChange(e.target.checked ? [...chosen, t] : chosen.filter((x) => x !== t))}
              className="accent-[var(--brand)]"
            />
            {TOKEN_LABEL[t as keyof typeof TOKEN_LABEL] ?? t}
          </label>
        ))}
      </div>
    </fieldset>
  )
}

function GateForm({ gate, current, canEdit, why, ask }: { gate: PolicyGate; current: HostPolicy; canEdit: boolean; why: string | null; ask: ReturnType<typeof useSettingsHandoff>['ask'] }) {
  const [draft, setDraft] = useState<HostPolicy | null>(null)
  const p = draft ?? current
  const label = GATE_LABEL[gate]
  const code = gate === 'code'
  const changed = !samePolicy(p, current)
  const problem = policyProblem(gate, p)
  const patch = (next: Partial<HostPolicy>) => setDraft({ ...p, ...next })
  const mode = p.applies === 'all' || p.applies === 'off' ? p.applies : 'types'
  const types = Array.isArray(p.applies) ? p.applies : []
  return (
    <Section title={label}>
      <p className="text-[13px]">{sentence(label, current)}</p>
      <div className="mt-3 space-y-3">
        <Toggles legend={`${label}: who may approve`} tokens={APPROVER_TOKENS} chosen={p.approvers} disabled={!canEdit} onChange={(approvers) => patch({ approvers })} />
        <Toggles legend={`${label}: who may not approve, even if listed above`} tokens={APPROVER_TOKENS} chosen={p.not} disabled={!canEdit} locked={code ? ['assignees'] : []} onChange={(not) => patch({ not })} />
        <div className="flex flex-wrap items-end gap-4">
          <div className="space-y-1.5">
            <Label htmlFor={`count-${gate}`}>{label}: approvals needed</Label>
            <Input id={`count-${gate}`} type="number" min={1} max={9} step={1} className="w-24" value={Number.isNaN(p.count) ? '' : p.count} disabled={!canEdit} onChange={(e) => patch({ count: e.target.value === '' ? NaN : Number(e.target.value) })} />
          </div>
          <label className="flex items-center gap-2 pb-2 text-[13px]">
            <input type="checkbox" checked={p.independent} disabled={!canEdit || code} onChange={(e) => patch({ independent: e.target.checked })} className="accent-[var(--brand)]" />
            {label}: approvers independent of the assignees' work{code ? ' (always, for the code review)' : ''}
          </label>
        </div>
        {code && (
          <fieldset className="space-y-1.5" disabled={!canEdit}>
            <legend className="text-[12px] text-text-muted">Where the code review applies</legend>
            {(
              [
                ['off', 'Off'],
                ['all', 'Every ticket'],
                ['types', 'Only these ticket types'],
              ] as const
            ).map(([v, text]) => (
              <label key={v} className="flex items-center gap-2 text-[13px]">
                <input type="radio" name={`applies-${gate}`} checked={mode === v} onChange={() => patch({ applies: v === 'types' ? (types.length ? types : ['feature']) : v })} className="accent-[var(--brand)]" />
                {text}
              </label>
            ))}
            <div role="group" aria-label="Ticket types that need a code review" className="ml-6 flex flex-wrap gap-3">
              {TICKET_TYPES.map((t) => (
                <label key={t} className="flex items-center gap-1.5 text-[13px]">
                  <input type="checkbox" checked={types.includes(t)} disabled={mode !== 'types'} onChange={(e) => patch({ applies: e.target.checked ? [...types, t] : types.filter((x) => x !== t) })} className="accent-[var(--brand)]" />
                  {t}
                </label>
              ))}
            </div>
          </fieldset>
        )}
      </div>
      {problem && changed && (
        <p role="alert" className="mt-3 text-[13px] text-danger">
          {problem}
        </p>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button
          disabled={!canEdit || !changed || !!problem}
          onClick={() =>
            ask({
              title: `Change the ${label.toLowerCase()} gate`,
              action: { action: 'policy_set', gate, policy: canonicalPolicy(p) },
              autoStart: true,
              form: null,
            })
          }
          aria-describedby={why ? `gate-${gate}-why` : undefined}
        >
          Change {label.toLowerCase()} policy
        </Button>
        {draft && changed && (
          <Button variant="ghost" onClick={() => setDraft(null)}>
            Discard changes
          </Button>
        )}
        {why && <OwnerNote id={`gate-${gate}-why`}>{why}</OwnerNote>}
      </div>
    </Section>
  )
}

/** The default policy of each gate (F1 5.7), read from the host and changed by a hand-off (`policy.set`, owners). */
export function HostGates({ workspace }: { workspace: Workspace }) {
  const read = useQuery(queries.hostSettings(workspace.id))
  const role = useViewerRole(workspace)
  const { ask, dialog } = useSettingsHandoff(workspace.id)
  if (read.isError) return <LoadFailed what="gate policies" onRetry={() => void read.refetch()} />
  if (!read.data) return <p className="text-[13px] text-text-muted">Reading the gate policies…</p>
  const data = read.data
  const served = hasFeature('do:policy.set', workspace.id)
  const why = !canChangeSettings(role) ? 'Only owners can change this gate.' : !served ? 'This host cannot change gate policies yet.' : null
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">Gate policies</h2>
        <p className="mt-1 text-[13px] text-text-muted">
          The workspace default for each gate. A ticket can only tighten it. A change to a gate's policy ends the approvals already given at that gate, on every ticket where it applies: they are given again under the new policy.
        </p>
        {data.policy_at && (
          <p className="mt-1 text-[12px] text-text-faint">
            Last changed {fmtDateTime(data.policy_at.at)} by <Raw>{workspace.members.find((m) => m.person === data.policy_at?.by)?.name ?? data.policy_at.by}</Raw>.
          </p>
        )}
      </div>
      {!data.available || !data.gates ? (
        <p data-testid="settings-unavailable" className="rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-text-muted">
          This host does not say what the gate policies are, so none is shown.
        </p>
      ) : (
        POLICY_GATES.map((g) => (data.gates?.[g] ? <GateForm key={`${g}-${JSON.stringify(data.gates[g])}`} gate={g} current={data.gates[g]} canEdit={!why} why={why} ask={ask} /> : null))
      )}
      {dialog}
    </div>
  )
}
