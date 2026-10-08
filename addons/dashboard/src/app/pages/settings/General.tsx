import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, type Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { fmtTime, Mono, Section } from '../ticket/shared'
import { DangerZone } from './DangerZone'

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
      await qc.invalidateQueries()
      setName(null)
      toast.success('Workspace renamed')
    } catch (e) {
      toast.error(e instanceof ApiError ? e.message : 'Could not rename')
    }
  }
  const id = identity.data

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">General</h2>
      <Section title="Workspace">
        <div className="flex items-end gap-2">
          <div className="max-w-sm flex-1 space-y-1.5">
            <Label htmlFor="ws-name">Name</Label>
            <Input id="ws-name" value={value} disabled={!canEdit} onChange={(e) => setName(e.target.value)} />
          </div>
          <Button disabled={!canEdit || value.trim() === workspace.name || !value.trim()} onClick={save}>
            Save
          </Button>
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

      <Section title="Relay">
        <div className="flex items-center gap-3 text-[13px]">
          <p className="flex-1 text-text-muted">Not connected · arrives with orch-relay (P3)</p>
          <Button variant="outline" size="sm" disabled>
            Connect
          </Button>
        </div>
      </Section>

      <DangerZone workspace={workspace} identity={id} canEdit={canEdit} />
    </div>
  )
}
