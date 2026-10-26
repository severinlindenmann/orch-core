import { HelpCircle, Trash2, UserPlus } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useId, useRef, useState } from 'react'
import type { KnownPerson, Role, Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { ago, PersonAvatar } from '../ticket/shared'
import { OwnerNote } from './OwnerNote'
import { useSettingsSign } from './useSettingsSign'
import { queries } from '@/api/queries'

const ROLES: Role[] = ['owner', 'maintainer', 'member', 'viewer']
const ROLE_HELP: Record<Role, string> = {
  owner: 'Everything: settings, roles, gate policies, grants, approvals the policy allows.',
  maintainer: 'Creates and moves tickets, issues grants, approves gates when the policy allows.',
  member: 'Creates tickets, comments, answers questions addressed to them, grants their own agents the tickets they may work on.',
  viewer: 'Reads everything they can see. Changes nothing.',
}

function RoleHelp() {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="sm" aria-label="What can each role do?">
          <HelpCircle />
          Roles
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 text-[13px]">
        <dl className="space-y-2">
          {ROLES.map((r) => (
            <div key={r}>
              <dt className="font-medium">{r}</dt>
              <dd className="text-text-muted">{ROLE_HELP[r]}</dd>
            </div>
          ))}
        </dl>
      </PopoverContent>
    </Popover>
  )
}

const ADD_ROLE_HELP: Record<Exclude<Role, 'owner'>, string> = {
  maintainer: 'Maintainer: can approve plans and verdicts, cannot change settings.',
  member: 'Member: can create tickets, comment and answer questions, cannot approve gates.',
  viewer: 'Viewer: reads what they can see, changes nothing.',
}
const ADD_ROLES = Object.keys(ADD_ROLE_HELP) as Exclude<Role, 'owner'>[]
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

/** Add by name or email: a combobox over the people this device knows. Enter adds the highlighted person (or the one chosen). */
function AddMember({ workspaceId, members, onSubmit, onClose }: { workspaceId: string; members: Workspace['members']; onSubmit: (v: { person: string; name: string; email: string; role: Exclude<Role, 'owner'> }) => void; onClose: () => void }) {
  const known = useQuery(queries.people(workspaceId))
  const [text, setText] = useState('')
  const [chosen, setChosen] = useState<KnownPerson | null>(null)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const [role, setRole] = useState<Exclude<Role, 'owner'>>('member')
  const [error, setError] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const arrowed = useRef(false)
  const listId = useId()

  const needle = text.trim().toLowerCase()
  const options = (known.data ?? []).filter((p) => !members.some((m) => m.person === p.person) && (!needle || p.name.toLowerCase().includes(needle) || p.email.toLowerCase().includes(needle)))
  const shown = open && !chosen

  const pick = (p: KnownPerson) => {
    setChosen(p)
    setText(p.name)
    setOpen(false)
    setError(null)
  }
  const submit = () => {
    const typed = text.trim()
    const who = chosen ?? (needle || arrowed.current ? options[active] : null) ?? (known.data ?? []).find((p) => p.email && p.email.toLowerCase() === typed.toLowerCase()) ?? null
    if (!who) {
      setError(EMAIL.test(typed) ? 'No one with this email in your directory yet — inviting by email comes later.' : typed ? 'Nobody known matches that. Pick a person from the list or type an email address.' : 'Choose a person, or type an email address.')
      input.current?.focus()
      return
    }
    if (members.some((m) => m.person === who.person)) {
      setError(`${who.name} is already a member.`)
      input.current?.focus()
      return
    }
    onSubmit({ person: who.person, name: who.name, email: who.email, role })
  }

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-sm gap-4 border-border bg-surface">
        <form
          noValidate
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault()
            submit()
          }}
        >
          <DialogHeader>
            <DialogTitle>Add member</DialogTitle>
            <DialogDescription>Owners are made by promoting a member afterwards.</DialogDescription>
          </DialogHeader>
          <div className="relative space-y-1.5">
            <Label htmlFor="member-person">Person</Label>
            <Input
              id="member-person"
              ref={input}
              role="combobox"
              aria-label="Person"
              aria-expanded={shown}
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={shown && options[active] ? `${listId}-${options[active].person}` : undefined}
              aria-invalid={!!error || undefined}
              aria-describedby={error ? 'member-person-error member-person-help' : 'member-person-help'}
              autoComplete="off"
              value={text}
              placeholder="Name or email"
              onFocus={() => setOpen(true)}
              onChange={(e) => {
                setText(e.target.value)
                setChosen(null)
                arrowed.current = false
                setOpen(true)
                setActive(0)
                setError(null)
              }}
              onKeyDown={(e) => {
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                  e.preventDefault()
                  arrowed.current = true
                  setOpen(true)
                  setActive((a) => Math.max(0, Math.min(options.length - 1, a + (e.key === 'ArrowDown' ? 1 : -1))))
                } else if (e.key === 'Enter' && shown && options[active]) {
                  // With the list open Enter picks the highlighted person; a second Enter (list closed, person chosen) adds.
                  e.preventDefault()
                  pick(options[active])
                } else if (e.key === 'Escape' && shown) {
                  e.stopPropagation()
                  setOpen(false)
                }
              }}
            />
            {shown && (
              <ul id={listId} role="listbox" aria-label="People" className="mt-1 max-h-40 overflow-y-auto rounded-md border border-border bg-surface p-1 shadow-lg">
                {options.length === 0 && <li className="px-2 py-1.5 text-[13px] text-text-faint">{needle ? 'Nobody in your directory matches.' : 'Everyone known is already a member.'}</li>}
                {options.map((p, i) => (
                  <li
                    key={p.person}
                    id={`${listId}-${p.person}`}
                    role="option"
                    aria-selected={i === active}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => pick(p)}
                    className={`flex cursor-pointer items-center justify-between gap-2 rounded px-2 py-1.5 text-[13px] ${i === active ? 'bg-surface-3' : 'hover:bg-surface-2'}`}
                  >
                    <span>{p.name}</span>
                    <span className="text-[12px] text-text-faint">{p.email}</span>
                  </li>
                ))}
              </ul>
            )}
            <p id="member-person-help" className="text-[12px] text-text-faint">
              Pick someone from your directory. Adding by an email address that is not in it comes later.
            </p>
            {error && (
              <p id="member-person-error" role="alert" className="text-[12px] text-danger">
                {error}
              </p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="member-role">Role</Label>
            <select id="member-role" value={role} onChange={(e) => setRole(e.target.value as Exclude<Role, 'owner'>)} className="h-9 w-full rounded-md border border-border bg-bg px-2 text-[13px]">
              {ADD_ROLES.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
            <ul aria-label="What each role can do" className="space-y-0.5 pt-1 text-[12px] text-text-faint">
              {ADD_ROLES.map((r) => (
                <li key={r} className={r === role ? 'text-text' : undefined}>
                  {ADD_ROLE_HELP[r]}
                </li>
              ))}
            </ul>
          </div>
          <DialogFooter className="gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">Add</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function Members({ workspace, viewer, canEdit }: { workspace: Workspace; viewer: string; canEdit: boolean }) {
  const { ask, prompt } = useSettingsSign(workspace.id)
  const [adding, setAdding] = useState(false)
  const owners = workspace.members.filter((m) => m.role === 'owner').length

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="flex-1 text-base font-semibold">Members &amp; roles</h2>
        <RoleHelp />
        {canEdit && (
          <Button size="sm" onClick={() => setAdding(true)}>
            <UserPlus />
            Add member
          </Button>
        )}
      </div>

      {!canEdit && (
        <p>
          <OwnerNote>Only owners can change roles or add and remove members.</OwnerNote>
        </p>
      )}
      <div className="rounded-lg border border-border bg-surface">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Person</TableHead>
              <TableHead className="w-44">Role</TableHead>
              <TableHead>Devices</TableHead>
              <TableHead>Last seen</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {workspace.members.map((m) => {
              const lastOwner = m.role === 'owner' && owners === 1
              const locked = !canEdit || lastOwner
              const select = (
                <select
                  aria-label={`Role of ${m.name}`}
                  value={m.role}
                  disabled={locked}
                  onChange={(e) => {
                    const role = e.target.value as Role
                    ask({
                      title: `Make ${m.name} ${role === 'owner' ? 'an owner' : `a ${role}`}`,
                      covers: [`Person: ${m.name} (${m.person})`, `Role: ${m.role} to ${role}`, 'Takes effect for new actions at once'],
                      req: { op: 'member.role', person: m.person, role },
                    })
                  }}
                  className="h-8 w-full rounded-md border border-border bg-bg px-2 text-[13px] disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {ROLES.map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </select>
              )
              return (
                <TableRow key={m.person}>
                  <TableCell>
                    <span className="flex items-center gap-2">
                      <PersonAvatar id={m.person} name={m.name} />
                      {m.name}
                      <span className="font-mono text-[11px] text-text-faint">{m.person}</span>
                    </span>
                  </TableCell>
                  <TableCell>
                    {lastOwner && canEdit ? (
                      <TooltipProvider delayDuration={0}>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <span className="block">{select}</span>
                          </TooltipTrigger>
                          <TooltipContent>You cannot demote the last owner. Make someone else an owner first.</TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    ) : (
                      select
                    )}
                  </TableCell>
                  <TableCell className="tabular-nums">{m.devices ?? 0}</TableCell>
                  <TableCell className="text-text-muted">{m.last_seen ? ago(m.last_seen) : 'never'}</TableCell>
                  <TableCell>
                    {canEdit && (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Remove ${m.name}`}
                        disabled={m.person === viewer}
                        title={m.person === viewer ? 'You cannot remove yourself.' : undefined}
                        onClick={() =>
                          ask({
                            title: `Remove ${m.name}`,
                            covers: [`Person: ${m.name} (${m.person})`, `Loses the ${m.role} role in ${workspace.prefix}`, 'Their open claims are not touched'],
                            req: { op: 'member.remove', person: m.person },
                            destructive: true,
                          })
                        }
                      >
                        <Trash2 />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      {adding && (
        <AddMember
          workspaceId={workspace.id}
          members={workspace.members}
          onClose={() => setAdding(false)}
          onSubmit={(v) => {
            setAdding(false)
            ask({
              title: `Add ${v.name}`,
              covers: [`Person: ${v.name} (${v.email}, ${v.person})`, `Role: ${v.role}`, 'Joins this workspace now'],
              req: { op: 'member.add', person: v.person, name: v.name, role: v.role },
            })
          }}
        />
      )}
      {prompt}
    </div>
  )
}
