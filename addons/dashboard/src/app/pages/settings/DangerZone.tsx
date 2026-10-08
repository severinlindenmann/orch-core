import { Download } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import { ApiError, type Workspace, type WorkspaceIdentity } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Section } from '../ticket/shared'

/** Mock download: the workspace and its identity as JSON. */
function exportWorkspace(workspace: Workspace, identity: WorkspaceIdentity | undefined) {
  const blob = new Blob([JSON.stringify({ workspace, identity }, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${workspace.prefix.toLowerCase()}-workspace.json`
  a.click()
  URL.revokeObjectURL(url)
}

export function DangerZone({ workspace, identity, canEdit }: { workspace: Workspace; identity: WorkspaceIdentity | undefined; canEdit: boolean }) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [refusal, setRefusal] = useState<string | null>(null)

  const close = () => {
    setOpen(false)
    setTyped('')
    setRefusal(null)
  }
  const archive = async () => {
    try {
      await api.postSettings(workspace.id, { op: 'archive', prefix: typed })
    } catch (e) {
      setRefusal(e instanceof ApiError ? e.message : 'Could not archive')
    }
  }

  return (
    <Section title="Danger zone" className="border-danger/40">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" size="sm" onClick={() => exportWorkspace(workspace, identity)}>
          <Download />
          Export workspace
        </Button>
        <Button variant="destructive" size="sm" disabled={!canEdit} onClick={() => setOpen(true)}>
          Archive workspace
        </Button>
      </div>
      {open && (
        <Dialog open onOpenChange={(o) => !o && close()}>
          <DialogContent className="max-w-sm gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Archive {workspace.name}</DialogTitle>
              <DialogDescription>The workspace becomes read-only for everyone.</DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="archive-prefix">Type {workspace.prefix} to confirm</Label>
              <Input id="archive-prefix" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
            </div>
            {refusal && (
              <p role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-[13px] text-danger">
                {refusal}
              </p>
            )}
            <DialogFooter className="gap-2">
              <Button variant="ghost" onClick={close}>
                Cancel
              </Button>
              <Button variant="destructive" disabled={typed !== workspace.prefix} onClick={archive}>
                Archive
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </Section>
  )
}
