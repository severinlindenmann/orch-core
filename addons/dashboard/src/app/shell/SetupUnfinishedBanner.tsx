// "Setup unfinished: one-click is not on yet" (B11 finding 7). Setup turns the one-click flag on and moves the key
// last; when the move fails the key stays under its passphrase and every signature is done in the terminal. The
// signer's card shows exactly that state (flag on, key not in one-click custody), so the banner needs no extra record.
// Nothing here signs or changes anything: "Finish setup" says what to run.
import { Info } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { Workspace } from '@/api/types'
import { useWorkspace } from '../workspace'

/** The flag is on but the key is not in one-click custody: setup began one-click and did not finish it. */
export const oneClickUnfinished = (click: Workspace['click'] | undefined): boolean => !!click && click.enabled && click.custody !== 'convenience'

export function SetupUnfinishedBanner() {
  const { workspace } = useWorkspace()
  if (!workspace || !oneClickUnfinished(workspace.click)) return null
  return <SetupUnfinishedLine />
}

export function SetupUnfinishedLine() {
  const [open, setOpen] = useState(false)
  return (
    <div role="region" aria-label="Setup unfinished" data-testid="setup-unfinished-banner" className="flex min-h-9 shrink-0 items-start gap-2 border-b border-border bg-surface px-4 py-2 text-[13px]">
      <Info className="mt-0.5 size-4 shrink-0 text-text-muted" aria-hidden />
      <span className="min-w-0 flex-1 whitespace-normal text-text">Setup unfinished: one-click is not on yet. Until then you approve in the terminal.</span>
      <Button size="xs" variant="outline" className="shrink-0" onClick={() => setOpen(true)}>
        Finish setup
      </Button>
      {open && (
        <Dialog open onOpenChange={(o) => !o && setOpen(false)}>
          <DialogContent className="max-w-md gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Finish setup</DialogTitle>
              <DialogDescription>
                One-click could not be set up when this workspace was created, so your key stays under its passphrase and you approve in the terminal.
              </DialogDescription>
            </DialogHeader>
            <ol className="list-decimal space-y-1 pl-5 text-[13px] text-text">
              <li>Open a terminal in this workspace’s project folder.</li>
              <li>
                Run <code className="rounded bg-surface-2 px-1">orch setup</code>. It offers the missing step, and asks for your passphrase on the terminal.
              </li>
            </ol>
            <DialogFooter>
              <Button onClick={() => setOpen(false)}>Close</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}
