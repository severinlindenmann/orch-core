// The host's preview of a new terminal, shown before anything starts (shared by the dock and the Terminals page).

import { Inline, Prose, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import type { TermPreview } from '@/api/runs'
import { EXECUTION_LINE } from './executionLine'

/** The host's preview, shown before anything starts: what runs, where, as whom. Every value is untrusted text. */
export function PreviewDialog({ preview, busy, onClose, onConfirm }: { preview: { data: TermPreview | null; error: string } | null; busy: boolean; onClose: () => void; onConfirm: () => void }) {
  const data = preview?.data
  return (
    <Dialog open={!!preview} onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-w-lg gap-3 border-border bg-surface" data-term-preview>
        <DialogHeader>
          <DialogTitle>Open a terminal</DialogTitle>
          <DialogDescription>{EXECUTION_LINE}</DialogDescription>
        </DialogHeader>
        {!data && !preview?.error && <Skeleton className="h-20" />}
        {data && (
          <div className="space-y-2 text-xs" data-testid="term-preview-lines">
            <dl className="grid grid-cols-[110px_minmax(0,1fr)] gap-x-3 gap-y-1.5">
              <dt className="text-text-muted">In</dt>
              <dd>{data.cwd_path !== null ? <Raw>{data.cwd_path}</Raw> : <span className="text-text-muted">{data.cwd_path_hidden ? 'Hidden: the path has invisible characters' : 'Not shown'}</span>}</dd>
              <dt className="text-text-muted">As</dt>
              <dd>{data.notice ? <Inline>{data.notice}</Inline> : 'acts as you, not as an agent'}</dd>
            </dl>
            <details data-testid="term-details">
              <summary className="cursor-pointer text-text-muted">Details</summary>
              <dl className="mt-2 grid grid-cols-[110px_minmax(0,1fr)] gap-x-3 gap-y-1.5">
                <dt className="text-text-muted">Runs</dt>
                <dd>{data.display !== null ? <Raw>{data.display}</Raw> : <span className="text-text-muted">{data.display_hidden ? 'Hidden: the command has invisible characters' : 'Not shown'}</span>}</dd>
                {data.login_shell && (<><dt className="text-text-muted">Started like</dt><dd>You would start it yourself in a terminal, so your own shell settings apply first and can change what runs.</dd></>)}
                {data.env.length > 0 && (<><dt className="text-text-muted">Environment</dt><dd className="font-mono text-[11px]">{data.env.map((n) => <span key={n} className="mr-2 inline-block"><Inline>{n}</Inline></span>)}</dd></>)}
              </dl>
            </details>
          </div>
        )}
        {data?.warnings.map((w, i) => <p key={i} role="note" className="text-xs text-warning"><Prose inline>{w}</Prose></p>)}
        {data?.blocked && (
          <p role="alert" className="rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-xs text-danger">
            Cannot open: {data.blocked.message ? <Prose inline>{data.blocked.message}</Prose> : 'the host refused.'}{data.blocked.code && <span className="ml-1 font-mono text-[11px]">({data.blocked.code})</span>}
          </p>
        )}
        {preview?.error && <p role="alert" className="text-xs text-danger"><Inline>{preview.error}</Inline></p>}
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" disabled={!data || !!data.blocked || busy} onClick={onConfirm}>Open terminal</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
