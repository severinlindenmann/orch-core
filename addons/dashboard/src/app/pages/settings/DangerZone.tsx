import { Download } from 'lucide-react'
import { type Workspace, type WorkspaceIdentity } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Section } from '../ticket/shared'
import { OwnerNote } from './OwnerNote'

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
  return (
    <Section title="Export">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" size="sm" disabled={!canEdit} aria-describedby={canEdit ? undefined : 'export-why'} onClick={() => exportWorkspace(workspace, identity)}>
          <Download />
          Export workspace
        </Button>
        {!canEdit && <OwnerNote id="export-why">Only owners can export.</OwnerNote>}
      </div>
    </Section>
  )
}
