// The ticket page's view of its worktrees (real host): per linked repository either its own folder (state, changes) or
// "shared working copy". Shown only where the host serves worktrees; adding and removing use the page's dialogs.
import { useQuery } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { FolderGit2 } from 'lucide-react'
import { useState } from 'react'
import { hasFeature, isRealHost } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { changesText, SHARED_COPY, STATE_TEXT, worktreeQueries, type Worktree } from '@/api/worktrees'
import type { TicketDocument } from '@/api/types'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { CreateWorktree, RemoveWorktree } from './Dialogs'

export function TicketWorktree({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<Worktree | null>(null)
  const served = isRealHost() && !!ws && hasFeature('worktrees', ws)
  const list = useQuery({ ...worktreeQueries.ofTicket(ws ?? '', ticket.key), enabled: served })
  const repos = ticket.links.repos
  if (!served || !ws || repos.length === 0 || !list.data?.available) return null
  const mayChange = atLeast(role, 'member') && ticket.status !== 'done'
  const mine = new Map(list.data.rows.map((r) => [r.repo, r]))
  const hasFree = repos.some((r) => !mine.has(r) && ticket.links.branches[r])
  return (
    <section aria-label="Worktree" className="space-y-2 rounded-lg border border-border bg-surface p-3" data-testid="ticket-worktree">
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-text-faint">Worktree</h3>
      <ul className="space-y-2 text-[13px]">
        {repos.map((repo) => {
          const w = mine.get(repo)
          const s = w ? STATE_TEXT[w.state] : null
          return (
            <li key={repo} className="flex items-start gap-2">
              <FolderGit2 className="mt-0.5 size-3.5 shrink-0 text-text-muted" aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block text-text-faint text-[11px]">
                  <Inline>{repo}</Inline>
                </span>
                {w && s ? (
                  <>
                    <span className="block">
                      {s.label} · {changesText(w)}
                    </span>
                    {!s.usable && <span className="block text-warning">{s.detail}</span>}
                    {w.path && (
                      <InfoDrawer>
                        <span className="block break-all font-mono">
                          <Inline>{w.path}</Inline>
                        </span>
                      </InfoDrawer>
                    )}
                  </>
                ) : (
                  <span className="block text-text-muted">Shared working copy</span>
                )}
              </span>
              {w && mayChange && (
                <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`Remove the worktree in ${repo}`} onClick={() => setRemoving(w)}>
                  Remove
                </Button>
              )}
            </li>
          )
        })}
      </ul>
      {mine.size === 0 && <p className="text-[12px] text-text-muted">{SHARED_COPY}</p>}
      {mayChange && hasFree && (
        <Button size="sm" variant="secondary" onClick={() => setAdding(true)}>
          Add worktree
        </Button>
      )}
      {adding && <CreateWorktree ws={ws} ticket={ticket.key} onClose={() => setAdding(false)} />}
      {removing && <RemoveWorktree ws={ws} row={removing} onClose={() => setRemoving(null)} />}
    </section>
  )
}
