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
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'

export function WorkFolder({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const qc = useQueryClient()
  const [asking, setAsking] = useState(false)
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
          <Button size="sm" variant="ghost" className="h-7 text-[12px]" onClick={() => setAsking(true)}>
            Delete work folder
          </Button>
        )}
      </div>
      {asking && (
        <DeleteWorkFolder
          ws={ws}
          ticket={ticket.key}
          summary={folderSummary(f)}
          onClose={() => setAsking(false)}
          onDone={() => qc.invalidateQueries({ queryKey: ['workdir', ws, ticket.key] })}
        />
      )}
    </section>
  )
}

function DeleteWorkFolder({ ws, ticket, summary, onClose, onDone }: { ws: string; ticket: string; summary: string; onClose: () => void; onDone: () => Promise<unknown> }) {
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
      onClose()
    } catch (e) {
      setError(workdirError(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="max-w-md gap-3 border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Delete this work folder?</DialogTitle>
          <DialogDescription>
            All the files in the work folder of <Inline>{ticket}</Inline> ({summary}) are deleted from this computer. This cannot be undone. Files that were added to the ticket as artifacts stay, and nothing else in your project is touched.
          </DialogDescription>
        </DialogHeader>
        {error && (
          <p role="alert" className="text-[13px] text-danger">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => void go()} disabled={busy}>
            {busy ? 'Deleting…' : 'Delete work folder'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
