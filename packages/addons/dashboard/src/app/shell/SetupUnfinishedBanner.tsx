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
    <div role="region" aria-label="Finish turning on" data-testid="setup-unfinished-banner" className="flex min-h-9 shrink-0 items-start gap-2 border-b border-border bg-surface px-4 py-2 text-[13px]">
      <Info className="mt-0.5 size-4 shrink-0 text-text-muted" aria-hidden />
      <span className="min-w-0 flex-1 whitespace-normal text-text">One click needs one more step. Finish on this computer to approve with one click again.</span>
      <Button size="xs" variant="outline" className="shrink-0" onClick={() => setOpen(true)}>
        Finish turning on
      </Button>
      {open && (
        <Dialog open onOpenChange={(o) => !o && setOpen(false)}>
          <DialogContent className="max-w-md gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Finish turning on</DialogTitle>
              <DialogDescription>
                One click is switched on, but your key is still under its passphrase, so you approve in the terminal. One quick step on this computer fixes that.
              </DialogDescription>
            </DialogHeader>
            <ol className="list-decimal space-y-1 pl-5 text-[13px] text-text">
              <li>Open Settings, General.</li>
              <li>Switch One click on and type your passphrase in the dialog.</li>
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
