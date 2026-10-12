import { useQuery } from '@tanstack/react-query'
import { HelpCircle, Trash2, UserPlus } from 'lucide-react'
import { useRef, useState } from 'react'
import { MAX_CERT_BYTES, MAX_KEY_FILE_BYTES, PUBLIC_KEY, canAddRole, canChangeRoles, canRemoveMember, certProblem, keyText, type RosterMember } from '@/api/settings'
import { hasFeature } from '@/api/features'
import type { Role, Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { queries } from '@/api/queries'
import { PersonAvatar } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useViewerPerson, useViewerRole } from './useViewerRole'
import { useSettingsHandoff } from './useSettingsHandoff'

const ROLES: Role[] = ['owner', 'maintainer', 'member', 'viewer']
const ROLE_HELP: Record<Role, string> = {
  owner: 'Everything: settings, roles, gate policies, grants, approvals the policy allows.',
  maintainer: 'Creates and moves tickets, issues grants, adds and removes members and viewers, approves gates when the policy allows.',
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

/** One file the person chooses: read as text here, size-checked before reading, never sent anywhere but the hand-off. */
function FileField({ id, label, help, value, onChange, limit, rows, error }: { id: string; label: string; help: string; value: string; onChange: (text: string) => void; limit: number; rows: number; error: string | null }) {
  const picker = useRef<HTMLInputElement>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const pick = async (file: File | undefined) => {
    if (!file) return
    if (file.size > limit) {
      setFileError(`${file.name} is larger than ${limit} bytes, so it is not what this field takes.`)
      return
    }
    setFileError(null)
    onChange(await file.text())
  }
  const shown = fileError ?? error
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <Label htmlFor={id} className="flex-1">
          {label}
        </Label>
        <input ref={picker} type="file" aria-label={`${label} file`} className="sr-only" tabIndex={-1} onChange={(e) => void pick(e.target.files?.[0])} />
        <Button type="button" variant="outline" size="xs" onClick={() => picker.current?.click()}>
          Choose file
        </Button>
      </div>
      <Textarea id={id} rows={rows} value={value} spellCheck={false} className="font-mono text-[12px]" aria-invalid={!!shown || undefined} aria-describedby={`${id}-help`} onChange={(e) => onChange(e.target.value)} />
      <p id={`${id}-help`} className="text-[12px] text-text-faint">
        {help}
      </p>
      {shown && (
        <p role="alert" className="text-[12px] text-danger">
          {shown}
        </p>
      )}
    </div>
  )
}

/** Adds a member from the invitee's own files (design 14 Q7): their public key and first device certificate. */
function AddMember({ me, onSubmit, onClose }: { me: Role | undefined; onSubmit: (v: { name: string; role: Role; pk: string; cert: string }) => void; onClose: () => void }) {
  const roles = (['maintainer', 'member', 'viewer'] as const).filter((r) => canAddRole(me, r))
  const [name, setName] = useState('')
  const [role, setRole] = useState<Role>(roles.includes('member') ? 'member' : roles[0])
  const [pk, setPk] = useState('')
  const [cert, setCert] = useState('')
  const [tried, setTried] = useState(false)
  const key = keyText(pk)
  const keyError = !key ? 'Choose or paste their public key.' : !PUBLIC_KEY.test(key) ? 'A public key is 87 characters of base64url (65 bytes).' : null
  const certError = !cert.trim() ? 'Choose or paste their device certificate.' : certProblem(cert)
  const nameError = !name.trim() ? 'Give them a name.' : name !== name.trim() || /[\u0000-\u001f\u007f\u2028\u2029]/.test(name) || name.length > 64 ? 'A name is one line of at most 64 characters, without spaces at its ends.' : null
  const submit = () => {
    setTried(true)
    if (keyError || certError || nameError) return
    onSubmit({ name, role, pk: key, cert })
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg gap-4 border-border bg-surface">
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
            <DialogDescription>
              The person makes their own key and first device; you sign only that they join. Ask them for their public key and device certificate files. Owners are made by promoting a member afterwards.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="member-name">Name</Label>
            <Input id="member-name" value={name} autoComplete="off" aria-invalid={(tried && !!nameError) || undefined} onChange={(e) => setName(e.target.value)} />
            {tried && nameError && (
              <p role="alert" className="text-[12px] text-danger">
                {nameError}
              </p>
            )}
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="member-role">Role</Label>
            <select id="member-role" value={role} onChange={(e) => setRole(e.target.value as Role)} className="h-9 w-full rounded-md border border-border bg-bg px-2 text-[13px]">
              {roles.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </div>
          <FileField id="member-pk" label="Public key" help="base64url text, 87 characters." value={pk} onChange={setPk} limit={MAX_KEY_FILE_BYTES} rows={2} error={tried ? keyError : null} />
          <FileField id="member-cert" label="Device certificate" help="Their first device certificate, a JSON file of at most 16 KiB." value={cert} onChange={setCert} limit={MAX_CERT_BYTES} rows={5} error={tried ? certError : null} />
          <p className="text-[12px] text-text-muted">The next step shows what the host reads in the certificate: the person id, the device and what it may do. Check with them, outside this dashboard, that the person id is theirs.</p>
          <DialogFooter className="gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">Prepare to add</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Members and roles from the host's roster. Role changes, additions and removals are hand-offs; the host decides who may sign. */
export function HostMembers({ workspace }: { workspace: Workspace }) {
  const roster = useQuery(queries.roster(workspace.id))
  const role = useViewerRole(workspace)
  const viewer = useViewerPerson()
  const { ask, dialog } = useSettingsHandoff(workspace.id)
  const [adding, setAdding] = useState(false)
  if (roster.isError) return <LoadFailed what="members" onRetry={() => void roster.refetch()} />
  if (!roster.data) return <p className="text-[13px] text-text-muted">Reading the members…</p>
  const members: RosterMember[] = roster.data
  const owners = members.filter((m) => m.role === 'owner').length
  const mayRole = canChangeRoles(role) && hasFeature('do:member.role', workspace.id)
  const mayAdd = (['maintainer', 'member', 'viewer'] as const).some((r) => canAddRole(role, r)) && hasFeature('do:member.add', workspace.id)
  const mayRemove = (target: Role) => canRemoveMember(role, target) && hasFeature('do:member.remove', workspace.id)

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="flex-1 text-base font-semibold">Members &amp; roles</h2>
        <RoleHelp />
        {mayAdd && (
          <Button size="sm" onClick={() => setAdding(true)}>
            <UserPlus />
            Add member
          </Button>
        )}
      </div>
      {!mayRole && (
        <p>
          <OwnerNote>{canChangeRoles(role) ? 'This host cannot change roles yet.' : 'Only owners can change roles. Owners add and remove anyone; maintainers add and remove members and viewers.'}</OwnerNote>
        </p>
      )}
      <div className="rounded-lg border border-border bg-surface">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Person</TableHead>
              <TableHead className="w-44">Role</TableHead>
              <TableHead>Devices</TableHead>
              <TableHead className="w-24" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {members.map((m) => {
              const lastOwner = m.role === 'owner' && owners === 1
              const select = (
                <select
                  aria-label={`Role of ${m.name}`}
                  value={m.role}
                  disabled={!mayRole || lastOwner}
                  title={lastOwner ? 'You cannot demote the last owner. Make someone else an owner first.' : undefined}
                  onChange={(e) => {
                    const next = e.target.value as Role
                    ask({ title: `Make ${m.name} ${next === 'owner' ? 'an owner' : `a ${next}`}`, action: { action: 'member_role', person: m.person, role: next }, autoStart: true })
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
                      <Raw>{m.name}</Raw>
                      <span className="font-mono text-[11px] text-text-faint"><Raw>{m.person}</Raw></span>
                    </span>
                  </TableCell>
                  <TableCell>{select}</TableCell>
                  <TableCell>
                    {m.devices.length === 0 ? (
                      <span className="text-text-faint">none</span>
                    ) : (
                      <ul aria-label={`Devices of ${m.name}`} className="space-y-0.5 text-[12px]">
                        {m.devices.map((d) => (
                          <li key={d.id} className="flex flex-wrap gap-x-2">
                            <span className="font-mono text-text-muted"><Raw>{d.id}</Raw></span>
                            <span className={d.valid ? 'text-text-faint' : 'text-warning'}>{d.valid ? <Raw>{d.scopes.join(', ')}</Raw> : 'not valid'}</span>
                            {d.auth && <span className="text-text-faint"><Raw>{d.auth}</Raw></span>}
                          </li>
                        ))}
                      </ul>
                    )}
                  </TableCell>
                  <TableCell>
                    {mayRemove(m.role) && (
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Remove ${m.name}`}
                        disabled={m.person === viewer || lastOwner}
                        title={m.person === viewer ? 'You cannot remove yourself.' : lastOwner ? 'The last owner cannot be removed.' : undefined}
                        onClick={() => ask({ title: `Remove ${m.name}`, action: { action: 'member_remove', person: m.person }, autoStart: true })}
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
          me={role}
          onClose={() => setAdding(false)}
          onSubmit={(v) => {
            setAdding(false)
            ask({ title: `Add ${v.name}`, action: { action: 'member_add', ...v }, autoStart: true })
          }}
        />
      )}
      {dialog}
    </div>
  )
}
