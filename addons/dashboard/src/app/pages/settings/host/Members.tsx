import { useQuery } from '@tanstack/react-query'
import { UserPlus } from 'lucide-react'
import { useRef, useState } from 'react'
import { MAX_CERT_BYTES, MAX_KEY_FILE_BYTES, PUBLIC_KEY, canAddRole, canChangeRoles, canRemoveMember, certProblem, deviceBelow, deviceLine, keyText, rightsOf, type RosterMember } from '@/api/settings'
import { hasFeature } from '@/api/features'
import type { Role, Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Raw, visible } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { queries } from '@/api/queries'
import { PersonAvatar } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useViewerPerson, useViewerRole } from './useViewerRole'
import { useSettingsHandoff } from './useSettingsHandoff'

const ROLES: Role[] = ['owner', 'maintainer', 'member', 'viewer']
const ROLE_LABEL: Record<Role, string> = { owner: 'Owner', maintainer: 'Maintainer', member: 'Member', viewer: 'Viewer' }
const ROLE_HELP: Record<Role, string> = {
  owner: 'Everything, including settings and people',
  maintainer: 'Manages tickets, agent access and members',
  member: 'Creates tickets and gives their own agents access',
  viewer: 'Can read, cannot change',
}

/** The four roles in plain words: a strip on a wide page, one fold on a narrow one. */
function RoleStrip() {
  return (
    <>
      <dl data-testid="role-strip" className="hidden grid-cols-4 gap-2 @[34rem]/page:grid">
        {ROLES.map((r) => (
          <div key={r} className="rounded-lg border border-border bg-surface p-3 text-[12px]">
            <dt className="mb-1 text-[13px] font-medium text-text">{ROLE_LABEL[r]}</dt>
            <dd className="text-text-muted">{ROLE_HELP[r]}</dd>
          </div>
        ))}
      </dl>
      <details className="rounded-lg border border-border bg-surface px-3 py-2 text-[13px] @[34rem]/page:hidden">
        <summary className="cursor-pointer">What each role can do</summary>
        <dl className="mt-2 space-y-2">
          {ROLES.map((r) => (
            <div key={r}>
              <dt className="font-medium">{ROLE_LABEL[r]}</dt>
              <dd className="text-text-muted">{ROLE_HELP[r]}</dd>
            </div>
          ))}
        </dl>
      </details>
    </>
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
            <DialogTitle>Add person</DialogTitle>
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
  const readers = useQuery({ ...queries.readers(workspace.id), enabled: hasFeature('readers', workspace.id) })
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
  // The signing device is never flagged: when one click is on and works, this computer signs now.
  const signsHere = !!workspace.click?.enabled && !!workspace.click.available
  const required = readers.data?.available === false ? null : (readers.data?.required_reader ?? null)

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="flex-1 text-base font-semibold">People</h2>
        {mayAdd && (
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
            <UserPlus />
            Add person
          </Button>
        )}
      </div>
      <RoleStrip />
      {!mayRole && (
        <p>
          <OwnerNote>{canChangeRoles(role) ? 'This host cannot change roles yet.' : 'Only owners can change roles. Owners add and remove anyone; maintainers add and remove members and viewers.'}</OwnerNote>
        </p>
      )}
      <ul aria-label="People" className="space-y-2">
        {members.map((m, idx) => {
          const lastOwner = m.role === 'owner' && owners === 1
          const mine = m.person === viewer
          const below = deviceBelow(m.devices, required, mine && signsHere)
          const rights = rightsOf(m.devices)
          const name = visible(m.name)
          return (
            <li key={m.person} data-testid="person-card" className="rounded-lg border border-border bg-surface p-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <PersonAvatar id={m.person} name={m.name} size="default" />
                <div className="min-w-0 flex-1 basis-40">
                  <div className="truncate text-[14px] font-medium">
                    {m.name_hidden ? <span className="text-warning">Name not shown: it has invisible characters</span> : <bdi>{name}</bdi>}
                  </div>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-text-muted">
                    <span>{deviceLine(m.devices, mine)}</span>
                    {below && <button type="button" data-testid="device-needs-update" onClick={() => { const d = document.getElementById(`person-details-${idx}`) as HTMLDetailsElement | null; if (d) { d.open = true; d.scrollIntoView({ block: 'nearest' }) } }} className="rounded-full border border-warning/50 px-2 py-px text-[11px] text-warning hover:bg-warning/10">Device update needed</button>}
                  </div>
                </div>
                <select
                  aria-label={`Role of ${name}`}
                  value={m.role}
                  disabled={!mayRole || lastOwner}
                  title={lastOwner ? 'You cannot demote the last owner. Make someone else an owner first.' : undefined}
                  onChange={(e) => {
                    const next = e.target.value as Role
                    ask({ title: `Make ${name} ${next === 'owner' ? 'an owner' : `a ${next}`}`, action: { action: 'member_role', person: m.person, role: next }, autoStart: true })
                  }}
                  className="h-8 rounded-full border border-border bg-bg px-3 text-[13px] disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>{ROLE_LABEL[r]}</option>
                  ))}
                </select>
                {mayRemove(m.role) && (
                  <Button
                    variant="outline"
                    size="sm"
                    aria-label={`Remove ${name}`}
                    disabled={mine || lastOwner}
                    title={mine ? 'You cannot remove yourself.' : lastOwner ? 'The last owner cannot be removed.' : undefined}
                    onClick={() => ask({ title: `Remove ${name}`, action: { action: 'member_remove', person: m.person }, autoStart: true })}
                  >
                    Remove
                  </Button>
                )}
              </div>
              <details id={`person-details-${idx}`} className="mt-2 text-[12px] text-text-muted">
                <summary className="cursor-pointer">Details</summary>
                <div className="mt-2 space-y-2">
                  <ul aria-label={`What ${name} can do`} className="flex flex-wrap gap-1.5">
                    {rights.length === 0 ? <li>No device, so nothing yet.</li> : rights.map((r) => <li key={r} className="rounded-full border border-border px-2 py-px text-[11px] text-text">{r}</li>)}
                  </ul>
                  {below && <p>Update the orch app on {mine ? 'this computer' : 'that device'} before it can approve with One click.</p>}
                  <p className="break-all font-mono text-[11px] text-text-faint">Person id <Raw>{m.person}</Raw></p>
                  {m.devices.map((d, i) => (
                    <p key={d.id} className="break-all font-mono text-[11px] text-text-faint">
                      Device {i + 1} <Raw>{d.id}</Raw>{d.valid ? '' : ' · not valid'}{d.auth ? <> · <Raw>{d.auth}</Raw></> : null}
                    </p>
                  ))}
                </div>
              </details>
            </li>
          )
        })}
      </ul>
      {adding && (
        <AddMember
          me={role}
          onClose={() => setAdding(false)}
          onSubmit={(v) => {
            setAdding(false)
            ask({ title: `Add ${visible(v.name)}`, action: { action: 'member_add', ...v }, autoStart: true })
          }}
        />
      )}
      {dialog}
    </div>
  )
}
