import { HelpCircle, Trash2, UserPlus } from 'lucide-react'
import { useState } from 'react'
import type { Role, Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { ago, PersonAvatar } from '../ticket/shared'
import { useSettingsSign } from './useSettingsSign'

const ROLES: Role[] = ['owner', 'maintainer', 'member', 'viewer']
const ROLE_HELP: Record<Role, string> = {
  owner: 'Everything: settings, roles, gate policies, grants, approvals the policy allows.',
  maintainer: 'Creates and moves tickets, issues grants, approves gates when the policy allows.',
  member: 'Creates tickets, comments, answers questions addressed to them.',
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

function AddMember({ onSubmit, onClose }: { onSubmit: (v: { person: string; name: string; role: Exclude<Role, 'owner'> }) => void; onClose: () => void }) {
  const [name, setName] = useState('')
  const [person, setPerson] = useState('')
  const [role, setRole] = useState<Exclude<Role, 'owner'>>('member')
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-sm gap-4 border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Add member</DialogTitle>
          <DialogDescription>Owners are made by promoting a member afterwards.</DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <Label htmlFor="member-name">Name</Label>
          <Input id="member-name" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="member-person">Person id</Label>
          <Input id="member-person" value={person} onChange={(e) => setPerson(e.target.value)} placeholder="p_ida" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="member-role">Role</Label>
          <select id="member-role" value={role} onChange={(e) => setRole(e.target.value as Exclude<Role, 'owner'>)} className="h-9 w-full rounded-md border border-border bg-bg px-2 text-[13px]">
            {ROLES.filter((r) => r !== 'owner').map((r) => (
              <option key={r}>{r}</option>
            ))}
          </select>
        </div>
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!name.trim() || !person.trim()} onClick={() => onSubmit({ person: person.trim(), name: name.trim(), role })}>
            Add
          </Button>
        </DialogFooter>
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
                  aria-label="Role"
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
          onClose={() => setAdding(false)}
          onSubmit={(v) => {
            setAdding(false)
            ask({
              title: `Add ${v.name}`,
              covers: [`Person: ${v.name} (${v.person})`, `Role: ${v.role}`, 'Joins this workspace now'],
              req: { op: 'member.add', ...v },
            })
          }}
        />
      )}
      {prompt}
    </div>
  )
}
