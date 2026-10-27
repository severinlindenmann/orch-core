// The ticket page's line for the work folder (real host): where a ticket without a codebase keeps its working files,
// how much is in it, and "Delete work folder" (the person's own action, behind a confirmation). The files stay after the
// ticket closes until the person deletes them; results the agent filed are ticket artifacts and are not touched.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderOpen } from 'lucide-react'
import { useState } from 'react'
import { isRealHost } from '@/api/features'
import { atLeast } from '@/api/permissions'
import type { TicketDocument } from '@/api/types'
import { folderSummary, newIdem, workdirApi, workdirError, workdirQueries } from '@/api/workdir'
import { Inline } from '@/components/sign/visible'
import { PressAgainDelete } from '@/components/PressAgainDelete'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'

export function WorkFolder({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const qc = useQueryClient()
  const served = isRealHost() && !!ws
  const q = useQuery({ ...workdirQueries.ofTicket(ws ?? '', ticket.key), enabled: served })
  if (!served || !ws || !q.data?.available || q.data.codebase || !q.data.exists) return null
  const f = q.data
  return (
    <section aria-label="Work folder" className="space-y-2 rounded-lg border border-border bg-surface p-3" data-testid="ticket-workfolder">
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Work folder</h3>
      <div className="flex items-start gap-2 text-[13px]">
        <FolderOpen className="mt-0.5 size-3.5 shrink-0 text-text-muted" aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block">
            Work folder: {folderSummary(f)}, not artifacts
          </span>
          <span className="block text-[12px] text-text-muted">It stays after the ticket closes until you delete it. Files an agent added to the ticket are kept either way.</span>
          {f.folder && (
            <details className="text-[12px] text-text-muted">
              <summary className="cursor-pointer select-none">Technical details</summary>
              <span className="block break-all font-mono">
                <Inline>{f.folder}</Inline>
              </span>
            </details>
          )}
        </span>
        {atLeast(role, 'member') && (
          <DeleteWorkFolder key={`${ws}:${ticket.key}`} ws={ws} ticket={ticket.key} onDone={() => qc.invalidateQueries({ queryKey: ['workdir', ws, ticket.key] })} />
        )}
      </div>
    </section>
  )
}

function DeleteWorkFolder({ ws, ticket, onDone }: { ws: string; ticket: string; onDone: () => Promise<unknown> }) {
  const [idem] = useState(newIdem)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const go = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await workdirApi.remove(ws, ticket, idem)
      await onDone()
    } catch (e) {
      setError(workdirError(e))
    } finally {
      setBusy(false)
    }
  }
  return <div><PressAgainDelete disabled={busy} onDelete={() => void go()}>{busy ? 'Deleting…' : 'Delete work folder'}</PressAgainDelete>{error && <p role="alert" className="text-danger">{error}</p>}</div>
}
