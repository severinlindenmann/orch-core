import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { Link } from '@tanstack/react-router'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { fmtTime, Mono, Section } from '../ticket/shared'
import { grantDefaultHours, GRANT_MAX_HOURS } from '@/api/grants'
import { plural } from '@/lib/time'
import { DangerZone } from './DangerZone'
import { OwnerNote } from './OwnerNote'
import { toastApiError } from '@/app/toast'
import { useSettingsSign } from './useSettingsSign'

export function CopyButton({ value, label }: { value: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label={`Copy ${label}`}
      onClick={() => {
        void navigator.clipboard?.writeText(value)
        setDone(true)
        setTimeout(() => setDone(false), 1500)
      }}
    >
      {done ? <Check /> : <Copy />}
    </Button>
  )
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[140px_1fr] items-center gap-3 py-1.5 text-[13px]">
      <dt className="text-text-muted">{label}</dt>
      <dd className="flex items-center gap-1">{children}</dd>
    </div>
  )
}

export function General({ workspace, canEdit }: { workspace: Workspace; canEdit: boolean }) {
  const qc = useQueryClient()
  const identity = useQuery({ queryKey: ['identity', workspace.id], queryFn: () => api.getIdentity(workspace.id) })
  const [name, setName] = useState<string | null>(null)
  const value = name ?? workspace.name

  const save = async () => {
    try {
      await api.postSettings(workspace.id, { op: 'rename', name: value })
      // The name lives in the workspace list (switcher, palette, sidebar) and in the identity card here.
      await Promise.all([qc.invalidateQueries({ queryKey: ['workspaces'] }), qc.invalidateQueries({ queryKey: ['identity', workspace.id] })])
      setName(null)
      toast.success('Workspace renamed')
    } catch (e) {
      toastApiError(e, 'Could not rename')
    }
  }
  const id = identity.data
  const { ask, prompt } = useSettingsSign(workspace.id)
  const current = grantDefaultHours(workspace)
  const [hours, setHours] = useState<string | null>(null)
  const hoursValue = hours ?? String(current)
  const hoursNum = Number(hoursValue)
  const hoursValid = Number.isInteger(hoursNum) && hoursNum >= 1 && hoursNum <= GRANT_MAX_HOURS
  const saveHours = () =>
    ask({
      title: 'Change the agent grant length',
      covers: [
        `Default length of a grant: ${plural(hoursNum, 'hour')} (was ${plural(current, 'hour')})`,
        `Members can sign a grant for themselves up to ${plural(hoursNum, 'hour')}, for the tickets they may work on`,
        `Owners and maintainers can sign up to ${GRANT_MAX_HOURS} h for all tickets`,
        'Grants that are already signed keep their end time',
      ],
      req: { op: 'grant.hours', hours: hoursNum },
    })

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">General</h2>
      <Section title="Workspace">
        <div className="flex items-end gap-2">
          <div className="max-w-sm flex-1 space-y-1.5">
            <Label htmlFor="ws-name">Name</Label>
            <Input id="ws-name" name="workspace-name" value={value} disabled={!canEdit} onChange={(e) => setName(e.target.value)} />
          </div>
          <Button disabled={!canEdit || value.trim() === workspace.name || !value.trim()} onClick={save} aria-describedby={canEdit ? undefined : 'ws-name-why'}>
            Save
          </Button>
          {!canEdit && <OwnerNote id="ws-name-why">Only owners can save.</OwnerNote>}
        </div>
        <dl className="mt-3 divide-y divide-border">
          <Row label="Prefix">
            <Mono>{workspace.prefix}</Mono>
            <CopyButton value={workspace.prefix} label="prefix" />
          </Row>
          <Row label="UUID">
            <Mono>{id?.uuid ?? workspace.id}</Mono>
            <CopyButton value={id?.uuid ?? workspace.id} label="UUID" />
          </Row>
          <Row label="Key fingerprint">
            <Mono>{id?.key_fingerprint ?? '…'}</Mono>
            {id && <CopyButton value={id.key_fingerprint} label="key fingerprint" />}
          </Row>
          <Row label="Epoch">
            <span>epoch {id?.epoch ?? '…'}</span>
          </Row>
          <Row label="Created">{id ? fmtTime(id.created_at) : '…'}</Row>
        </dl>
      </Section>

      <Section title="Agent grants">
        <div className="flex items-end gap-2">
          <div className="max-w-[12rem] flex-1 space-y-1.5">
            <Label htmlFor="grant-length">Agent grant length (hours)</Label>
            <Input id="grant-length" name="grant-length" type="number" min={1} max={GRANT_MAX_HOURS} step={1} value={hoursValue} disabled={!canEdit} onChange={(e) => setHours(e.target.value)} />
          </div>
          <Button disabled={!canEdit || !hoursValid || hoursNum === current} onClick={saveHours} aria-describedby={canEdit ? undefined : 'grant-length-why'}>
            Save
          </Button>
          {!canEdit && <OwnerNote id="grant-length-why">Only owners can save.</OwnerNote>}
        </div>
        <p className="mt-2 text-[12px] text-text-muted">
          What a grant lasts when nobody picks a length, and the longest a member signs for themselves. Owners and maintainers can sign 1 to {GRANT_MAX_HOURS} h. Signed with Touch ID.
        </p>
      </Section>

      <Section title="Relay">
        <div className="flex items-center gap-3 text-[13px]">
          <p className="flex-1 text-text-muted">
            {workspace.relay === 'on' ? 'On (simulated)' : 'Not connected yet'} · devices, pairing and the sync queue are in Relay &amp; devices (Preview, simulated)
          </p>
          <Button variant="outline" size="sm" asChild>
            <Link to="/settings/$tab" params={{ tab: 'relay' }}>
              Open Relay &amp; devices
            </Link>
          </Button>
        </div>
      </Section>

      <DangerZone workspace={workspace} identity={id} canEdit={canEdit} />
      {prompt}
    </div>
  )
}
