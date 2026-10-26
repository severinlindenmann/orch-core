// "Files changed" on the ticket page (issue #491, decision 3): the files of the commits THIS ticket's own events name (a
// pushed branch, a finished task's receipt). Each opens in the explorer on that change. Owners only, while the add-on is on.
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { FileDiff } from 'lucide-react'
import { useState } from 'react'
import { explorerQueries, type ChangedFile } from '@/api/explorer'
import { hasFeature, isRealHost } from '@/api/features'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { useWorkspace } from '@/app/workspace'

const WORD: Record<ChangedFile['status'], string> = { A: 'new', M: 'changed', D: 'deleted', R: 'renamed', T: 'changed', C: 'copied' }
const SHOWN = 8

export function TicketFiles({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const served = isRealHost() && !!ws && hasFeature('explorer', ws)
  const q = useQuery({ ...explorerQueries.ticketFiles(ws ?? '', ticket.key), enabled: served })
  const [all, setAll] = useState(false)
  if (!served || !q.data || q.data.files.length === 0) return null
  const files = all ? q.data.files : q.data.files.slice(0, SHOWN)
  return (
    <section aria-label="Files changed" className="space-y-2 rounded-lg border border-border bg-surface p-3" data-testid="ticket-files">
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Files changed</h3>
      <ul className="space-y-1 text-[13px]">
        {files.map((f) => (
          <li key={`${f.root}:${f.path}`} className="flex items-start gap-2">
            <FileDiff className="mt-0.5 size-3.5 shrink-0 text-text-muted" aria-hidden />
            <Link
              to="/explorer"
              search={{ root: f.root, path: f.path, ...(f.status === 'D' ? { view: 'history' as const } : { view: 'history' as const, sha: f.sha }) }}
              className="min-w-0 flex-1 break-all hover:underline"
            >
              <bdi>{f.label}</bdi>
            </Link>
            <span className="shrink-0 text-[11px] text-text-faint">{WORD[f.status]}</span>
          </li>
        ))}
      </ul>
      {q.data.files.length > SHOWN && !all && (
        <Button size="sm" variant="ghost" className="h-7 text-[12px]" onClick={() => setAll(true)}>
          Show all {q.data.files.length}
        </Button>
      )}
    </section>
  )
}
