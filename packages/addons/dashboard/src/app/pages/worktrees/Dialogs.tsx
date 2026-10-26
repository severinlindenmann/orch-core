// Create and remove a worktree as the person. Both are dashboard-direct host writes (no signature): the host applies
// its own rules (who may, the Worktrees add-on, the helper that runs git, a rate limit) and says no in a code that this
// file turns into words. One idem per dialog opening, kept for every retry, so a retry never makes a second worktree.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { newIdem, worktreeError, worktreeQueries, worktreesApi, type Worktree } from '@/api/worktrees'
import { queries } from '@/api/queries'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'

export const refreshWorktrees = (qc: ReturnType<typeof useQueryClient>, ws: string) => qc.invalidateQueries({ queryKey: ['worktrees', ws] })

function Failure({ text }: { text: string }) {
  return (
    <p role="alert" className="text-[13px] text-danger">
      {text}
    </p>
  )
}

/** Remove: the folder goes; the branch and its commits stay. The host refuses a folder with unsaved changes. */
export function RemoveWorktree({ ws, row, onClose }: { ws: string; row: Worktree; onClose: () => void }) {
  const qc = useQueryClient()
  const [idem] = useState(newIdem)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const go = async () => {
    if (busy || !row.ticket) return
    setBusy(true)
    setError('')
    try {
      await worktreesApi.remove(ws, { ticket: row.ticket, ...(row.repo ? { repo: row.repo } : {}) }, idem)
      await refreshWorktrees(qc, ws)
      onClose()
    } catch (e) {
      setError(worktreeError(e))
      void refreshWorktrees(qc, ws)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="max-w-md gap-3 border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Remove this worktree?</DialogTitle>
          <DialogDescription>
            The separate folder for {row.ticket ? <Inline>{row.ticket}</Inline> : 'this ticket'} in {row.repo ? <Inline>{row.repo}</Inline> : 'its repository'} is deleted. The branch and everything saved on it stay. If the folder has changes that are not saved in git, it is not removed.
          </DialogDescription>
        </DialogHeader>
        {error && <Failure text={error} />}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => void go()} disabled={busy}>
            {busy ? 'Removing…' : 'Remove worktree'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** Create for a ticket. With `ticket` the ticket is fixed (the ticket page); without, the person picks one. */
export function CreateWorktree({ ws, ticket: fixed, onClose }: { ws: string; ticket?: string; onClose: () => void }) {
  const qc = useQueryClient()
  const [idem] = useState(newIdem)
  const [picked, setPicked] = useState(fixed ?? '')
  const [repo, setRepo] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const open = useQuery({ ...queries.tickets(ws, { status: ['open', 'in-progress', 'testing'] }), enabled: !fixed })
  const doc = useQuery({ ...queries.ticket(ws, picked), enabled: !!picked })
  const have = useQuery({ ...worktreeQueries.ofTicket(ws, picked), enabled: !!picked })
  const linked = doc.data?.links.repos ?? []
  const branches = doc.data?.links.branches ?? {}
  const taken = new Set((have.data?.rows ?? []).map((r) => r.repo))
  const free = linked.filter((r) => !taken.has(r))
  const chosen = repo && free.includes(repo) ? repo : free[0] ?? ''
  const reason = !picked
    ? ''
    : doc.isPending || have.isPending
      ? ''
      : linked.length === 0
        ? 'This ticket is not linked to a repository yet.'
        : free.length === 0
          ? 'This ticket already has a worktree for each of its repositories.'
          : !branches[chosen]
            ? 'This ticket has no branch for that repository yet. The branch is set first, then a worktree can be made.'
            : ''
  const go = async () => {
    if (busy || !picked || !chosen || reason) return
    setBusy(true)
    setError('')
    try {
      await worktreesApi.create(ws, { ticket: picked, repo: chosen }, idem)
      await refreshWorktrees(qc, ws)
      onClose()
    } catch (e) {
      setError(worktreeError(e))
      void refreshWorktrees(qc, ws)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="max-w-md gap-3 border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Add a worktree</DialogTitle>
          <DialogDescription>A worktree is a separate folder where one ticket’s work happens, so it never mixes with other tickets. It starts on the ticket’s branch.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3 text-[13px]">
          {!fixed && (
            <div className="space-y-1.5">
              <Label htmlFor="wt-ticket">Ticket</Label>
              <select id="wt-ticket" className="h-8 w-full rounded border border-border bg-bg px-2" value={picked} onChange={(e) => { setPicked(e.target.value); setRepo('') }}>
                <option value="">Pick a ticket…</option>
                {(open.data ?? []).map((t) => (
                  <option key={t.key} value={t.key}>
                    {t.key} · {t.title}
                  </option>
                ))}
              </select>
            </div>
          )}
          {picked && free.length > 1 && (
            <div className="space-y-1.5">
              <Label htmlFor="wt-repo">Repository</Label>
              <select id="wt-repo" className="h-8 w-full rounded border border-border bg-bg px-2" value={chosen} onChange={(e) => setRepo(e.target.value)}>
                {free.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
          )}
          {picked && !reason && chosen && (
            <p className="text-text-muted">
              Branch <Inline>{branches[chosen] ?? ''}</Inline> in <Inline>{chosen}</Inline>.
            </p>
          )}
          {reason && <p className="text-text-muted">{reason}</p>}
          {error && <Failure text={error} />}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void go()} disabled={busy || !picked || !chosen || !!reason}>
            {busy ? 'Creating…' : 'Create worktree'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
