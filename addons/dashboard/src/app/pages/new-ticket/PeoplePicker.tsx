import type { Member, Visibility } from '@/api/types'
import { can } from '@/api/permissions'
import { cn } from '@/lib/utils'

export interface PeopleValue {
  owner: string | null
  assignees: string[]
  reviewers: string[]
}

const chip = (on: boolean) =>
  cn(
    'inline-flex h-6 items-center rounded-md border px-2 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-brand',
    on ? 'border-brand bg-brand-soft text-brand' : 'border-input text-text-muted hover:bg-accent hover:text-text',
  )

export const fieldCls =
  'h-8 w-full rounded-md border border-input bg-transparent px-2 text-[13px] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50'

function Chips({ label, members, value, onChange }: { label: string; members: Member[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-1.5">
      {members.map((m) => {
        const on = value.includes(m.person)
        return (
          <button key={m.person} type="button" aria-pressed={on} className={chip(on)} onClick={() => onChange(on ? value.filter((p) => p !== m.person) : [...value, m.person])}>
            {m.name}
          </button>
        )
      })}
    </div>
  )
}

const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="space-y-1">
    <div className="text-[12px] text-text-muted">{label}</div>
    {children}
  </div>
)

/** Owner, assignees, reviewers and who can see the ticket. */
export function PeoplePicker({
  members,
  creator,
  people,
  onPeople,
  visibility,
  onVisibility,
}: {
  members: Member[]
  /** The person creating the ticket: a restricted ticket always starts with them (and the owners). */
  creator: string
  people: PeopleValue
  onPeople: (p: PeopleValue) => void
  visibility: Visibility
  onVisibility: (v: Visibility) => void
}) {
  const doers = members.filter((m) => can(m.role, 'ticket.act'))
  const restricted = typeof visibility === 'object'
  return (
    <div className="space-y-3">
      <Row label="Owner">
        <select aria-label="Owner" className={fieldCls} value={people.owner ?? ''} onChange={(e) => onPeople({ ...people, owner: e.target.value || null })}>
          <option value="">Nobody</option>
          {doers.map((m) => (
            <option key={m.person} value={m.person}>
              {m.name}
            </option>
          ))}
        </select>
      </Row>
      <Row label="Assignees">
        <Chips label="Assignees" members={doers} value={people.assignees} onChange={(assignees) => onPeople({ ...people, assignees })} />
      </Row>
      <Row label="Reviewers">
        <Chips label="Reviewers" members={doers} value={people.reviewers} onChange={(reviewers) => onPeople({ ...people, reviewers })} />
      </Row>
      <Row label="Visibility">
        <select
          aria-label="Visibility"
          className={fieldCls}
          value={restricted ? 'restricted' : 'workspace'}
          onChange={(e) => onVisibility(e.target.value === 'workspace' ? 'workspace' : { restricted: [...new Set([creator, ...members.filter((m) => m.role === 'owner').map((m) => m.person)])] })}
        >
          <option value="workspace">Workspace</option>
          <option value="restricted">Restricted to…</option>
        </select>
        {typeof visibility === 'object' && <Chips label="Can see this ticket" members={members} value={visibility.restricted} onChange={(restrictedTo) => onVisibility({ restricted: restrictedTo })} />}
      </Row>
    </div>
  )
}
