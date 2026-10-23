import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { APPROVAL_PRESETS, APPROVER_TOKENS, GATE_LABEL, POLICY_GATES, PRESET_LABEL, TICKET_TYPES, TOKEN_LABEL, canChangeSettings, canonicalPolicy, policyProblem, policyUnmet, presetOf, presetPolicy, samePolicy, type ApprovalPreset, type HostPolicy, type PolicyGate } from '@/api/settings'
import { hasFeature } from '@/api/features'
import type { Role, Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { fmtDateTime, plural } from '@/lib/time'
import { Switch } from '@/components/ui/switch'
import { queries } from '@/api/queries'
import { Raw } from '@/components/sign/visible'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useViewerPerson, useViewerRole } from './useViewerRole'
import { useSettingsHandoff } from './useSettingsHandoff'

const STEPS: readonly PolicyGate[] = ['requirements', 'plan', 'verify']
const STEP_QUESTION: Record<string, string> = { requirements: 'Who approves the requirements?', plan: 'Who approves the plan?', verify: 'Who checks the finished work?' }

const sentence = (label: string, p: HostPolicy) => {
  const who = p.approvers.map((a) => TOKEN_LABEL[a as keyof typeof TOKEN_LABEL] ?? a).join(', ')
  const not = p.not.length ? `, but not ${p.not.map((a) => (TOKEN_LABEL[a as keyof typeof TOKEN_LABEL] ?? a).toLowerCase()).join(' or ')}` : ''
  const where = p.applies === 'all' ? 'on every ticket' : p.applies === 'off' ? 'switched off' : `on ${p.applies.join(', ')} tickets`
  return `${label}: ${p.count} approval${p.count === 1 ? '' : 's'} from ${who.toLowerCase()}${not}, ${where}${p.independent ? ', independent' : ''}.`
}

/** One compact table per gate: a row per kind of person, one column for "may approve" and one for "never", so the
 * names are written once instead of twice (B11 31). */
function ApproverMatrix({ label, approvers, not, disabled, locked, onApprovers, onNot }: { label: string; approvers: string[]; not: string[]; disabled: boolean; locked?: string[]; onApprovers: (next: string[]) => void; onNot: (next: string[]) => void }) {
  const flip = (list: string[], t: string, on: boolean) => (on ? [...list, t] : list.filter((x) => x !== t))
  return (
    <fieldset className="min-w-0" disabled={disabled}>
      <table aria-label={`${label}: who may approve`} className="w-full max-w-md text-[13px]">
        <thead>
          <tr className="text-left text-[12px] text-text-muted">
            <th scope="col" className="py-1 pr-3 font-normal">Who</th>
            <th scope="col" className="px-2 py-1 font-normal">May approve</th>
            <th scope="col" className="px-2 py-1 font-normal">Never, even if allowed</th>
          </tr>
        </thead>
        <tbody>
          {APPROVER_TOKENS.map((t) => {
            const who = TOKEN_LABEL[t as keyof typeof TOKEN_LABEL] ?? t
            return (
              <tr key={t} className="border-t border-border">
                <th scope="row" className="py-1 pr-3 text-left font-normal">{who}</th>
                <td className="px-2 py-1"><input type="checkbox" aria-label={`${who} may approve`} checked={approvers.includes(t)} onChange={(e) => onApprovers(flip(approvers, t, e.target.checked))} className="accent-[var(--brand)]" /></td>
                <td className="px-2 py-1"><input type="checkbox" aria-label={`${who} may not approve`} checked={not.includes(t)} disabled={locked?.includes(t)} onChange={(e) => onNot(flip(not, t, e.target.checked))} className="accent-[var(--brand)]" /></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </fieldset>
  )
}

function GateForm({ gate, current, canEdit, why, ask, alone }: { alone?: boolean; gate: PolicyGate; current: HostPolicy; canEdit: boolean; why: string | null; ask: ReturnType<typeof useSettingsHandoff>['ask'] }) {
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
      {alone && gate === 'verify' && <p data-testid="gate-alone" className="mt-1 text-[12px] text-text-muted">You are the only member, so you can approve your own work for now. This rule starts to ask for someone else once another person joins.</p>}
      <div className="mt-3 space-y-3">
        <ApproverMatrix label={label} approvers={p.approvers} not={p.not} disabled={!canEdit} locked={code ? ['assignees'] : []} onApprovers={(approvers) => patch({ approvers })} onNot={(not) => patch({ not })} />
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

const PRESET_NOTE: Record<ApprovalPreset, string> = {
  owner: 'One owner approving is enough.',
  owner_or_maintainer: 'One owner or maintainer approving is enough.',
  two: 'Needs two people who can approve.',
  independent: 'An owner or maintainer who did not work on the ticket.',
}

/** One question: a preset select that signs the change, the plain consequence, and the "can never be met" block. */
function ApprovalStep({ gate, current, members, viewer, mayChange, why, ask }: { gate: PolicyGate; current: HostPolicy; members: readonly { person: string; role: Role }[]; viewer: string | undefined; mayChange: boolean; why: string | null; ask: ReturnType<typeof useSettingsHandoff>['ask'] }) {
  const label = GATE_LABEL[gate]
  const preset = presetOf(current)
  const owners = members.filter((m) => m.role === 'owner')
  const canApprove = members.filter((m) => m.role === 'owner' || m.role === 'maintainer').length
  const soleOwner = owners.length === 1 && owners[0].person === viewer
  const labelOf = (k: ApprovalPreset) => (k === 'owner' && soleOwner ? 'An owner (you)' : PRESET_LABEL[k])
  const unmet = policyUnmet(current, members)
  const id = `step-${gate}`
  return (
    <div className="rounded-lg border border-border bg-surface p-4" data-testid={`approval-${gate}`}>
      <Label htmlFor={id} className="text-[14px] font-medium">{STEP_QUESTION[gate] ?? label}</Label>
      <select
        id={id}
        value={preset ?? 'custom'}
        disabled={!mayChange}
        onChange={(e) => {
          const next = e.target.value as ApprovalPreset
          ask({ title: `Change who approves: ${label.toLowerCase()}`, action: { action: 'policy_set', gate, policy: presetPolicy(next, current) }, autoStart: true, form: null })
        }}
        className="mt-2 h-9 w-full rounded-md border border-border bg-bg px-2 text-[13px] disabled:cursor-not-allowed disabled:opacity-60"
        aria-describedby={why ? `${id}-why` : undefined}
      >
        {preset === null && <option value="custom" disabled>Custom rule (set under Custom rules)</option>}
        {APPROVAL_PRESETS.map((k) => (
          <option key={k} value={k} disabled={k === 'two' && canApprove < 2 && preset !== 'two'}>{labelOf(k)}</option>
        ))}
      </select>
      <p className="mt-2 text-[12px] text-text-muted">{preset ? PRESET_NOTE[preset] : sentence(label, current)}</p>
      {why && <OwnerNote id={`${id}-why`}>{why}</OwnerNote>}
      {unmet && (
        <div role="alert" data-testid={`approval-${gate}-unmet`} className="mt-3 space-y-1 rounded-md border border-warning/50 px-3 py-2 text-[13px]">
          <p className="font-medium text-warning">{plural(unmet.needs, 'approval')} required; only {unmet.has} {unmet.has === 1 ? 'person qualifies' : 'people qualify'}.</p>
          <p className="text-text-muted">Tickets would wait here for ever.</p>
          <Button type="button" size="sm" variant="outline" onClick={() => document.getElementById(id)?.focus()}>Change rule</Button>
        </div>
      )}
    </div>
  )
}

/** "Also review the code before it lands": a switch on the code gate (every ticket / off); other shapes stay under Custom rules. */
function CodeReviewSwitch({ current, mayChange, ask }: { current: HostPolicy; mayChange: boolean; ask: ReturnType<typeof useSettingsHandoff>['ask'] }) {
  const on = current.applies !== 'off'
  const some = Array.isArray(current.applies)
  const flip = () =>
    ask({
      title: on ? 'Turn the code review off' : 'Require a code review before changes are added',
      action: { action: 'policy_set', gate: 'code', policy: canonicalPolicy({ ...current, applies: on ? 'off' : 'all', not: current.not.includes('assignees') ? current.not : [...current.not, 'assignees'], independent: true }) },
      autoStart: true,
      form: null,
    })
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <div id="code-review-label" className="text-[14px] font-medium">Require a code review before changes are added</div>
          <p className="text-[12px] text-text-muted">Someone other than the person who did the work reviews the code.</p>
          {some && <p className="text-[12px] text-text-muted">Only for some ticket types. Change that under Custom rules.</p>}
        </div>
        <Switch checked={on} aria-labelledby="code-review-label" disabled={!mayChange} onCheckedChange={flip} />
      </div>
    </div>
  )
}

/** Who has to approve each step (F1 5.7): three questions with one choice each; the full matrix stays under Custom rules. Changes are hand-offs (`policy.set`, owners). */
export function HostGates({ workspace }: { workspace: Workspace }) {
  const read = useQuery(queries.hostSettings(workspace.id))
  const role = useViewerRole(workspace)
  const viewer = useViewerPerson()
  const { ask, dialog } = useSettingsHandoff(workspace.id)
  if (read.isError) return <LoadFailed what="approvals" onRetry={() => void read.refetch()} />
  if (!read.data) return <p className="text-[13px] text-text-muted">Reading the approval rules…</p>
  const data = read.data
  const served = hasFeature('do:policy.set', workspace.id)
  const why = !canChangeSettings(role) ? 'Only owners can change who approves.' : !served ? 'This host cannot change approvals yet.' : null
  const people = workspace.members
  const owners = people.filter((m) => m.role === 'owner').length
  const approvers = people.filter((m) => m.role === 'maintainer').length
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-base font-semibold">Who approves what?</h2>
        <div className="mt-1 text-[13px] text-text-muted">
          Pick who has to say yes at each step. A change asks everyone again at that step.
          <details className="mt-1 text-[12px]"><summary className="cursor-pointer">Details</summary>This workspace has {plural(owners, 'owner')}{approvers > 0 ? ` and ${plural(approvers, 'maintainer')}` : ''}. A ticket can only tighten these rules.</details>
        </div>
        {data.policy_at && (
          <p className="mt-1 text-[12px] text-text-faint">
            Last changed {fmtDateTime(data.policy_at.at)} by <Raw>{people.find((m) => m.person === data.policy_at?.by)?.name ?? data.policy_at.by}</Raw>.
          </p>
        )}
      </div>
      {!data.available || !data.gates ? (
        <p data-testid="settings-unavailable" className="rounded-md border border-border bg-surface px-3 py-2 text-[13px] text-text-muted">
          This host does not say what the approval rules are, so none is shown.
        </p>
      ) : (
        <>
          {STEPS.map((g) => (data.gates?.[g] ? <ApprovalStep key={`${g}-${JSON.stringify(data.gates[g])}`} gate={g} current={data.gates[g]} members={people} viewer={viewer} mayChange={!why} why={why} ask={ask} /> : null))}
          {data.gates.code && <CodeReviewSwitch current={data.gates.code} mayChange={!why} ask={ask} />}
          <details className="rounded-lg border border-border bg-surface px-4 py-3">
            <summary className="cursor-pointer text-[14px] font-medium">Custom rules (advanced)</summary>
            <div className="mt-3 space-y-4">
              {POLICY_GATES.map((g) => (data.gates?.[g] ? <GateForm key={`${g}-${JSON.stringify(data.gates[g])}`} gate={g} current={data.gates[g]} canEdit={!why} why={why} ask={ask} alone={people.length <= 1} /> : null))}
            </div>
          </details>
        </>
      )}
      {dialog}
    </div>
  )
}
