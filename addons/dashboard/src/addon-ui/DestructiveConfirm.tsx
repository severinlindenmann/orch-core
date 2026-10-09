import { useRef } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { addonName, FromAddon, wordsAndId } from './SignConfirm'

const MAX = 160
const cap = (v: string) => (v.length > MAX ? `${v.slice(0, MAX)}…` : v)

/**
 * Core's confirm for an action the manifest marks `confirm: 'destructive'`. It is not a signature. Trust split (as
 * SignConfirm): the title and the lines above the region are core's words (the action id, the addon's name, the
 * ticket); the package's words (label, sentence, the row's name) and the args it sends sit in the dashed "From the
 * addon" region. The button names the consequence with the manifest label ("Revoke link"), not "OK"; Cancel has focus.
 */
export function DestructiveConfirm({
  addon,
  addonTitle,
  action,
  label,
  text,
  subject,
  args,
  ticket,
  undoable,
  onConfirm,
  onClose,
}: {
  /** The addon action asked about; absent for core's own confirms (discarding unsaved edits), which are all core's words. */
  addon?: string
  addonTitle?: string
  action?: string
  label: string
  text?: string
  subject?: string
  args?: Record<string, unknown>
  ticket?: string
  /** The manifest declares an undo for it (`undo`): core says so instead of "This cannot be undone." */
  undoable?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  const cancel = useRef<HTMLButtonElement>(null)
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        role="alertdialog"
        className="max-w-md gap-4 border-border bg-surface"
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          cancel.current?.focus()
        }}
      >
        <DialogHeader>
          {addon && action ? (
            <>
              <DialogTitle>{confirmTitle(action, addonTitle ?? addon, addon)}</DialogTitle>
              <DialogDescription>
                {ticket ? `About ${ticket}. ` : ''}
                {/* Core's consequence line, from the manifest's undo pair (not from the addon's sentence). */}
                <span data-testid="consequence">{undoable ? 'You can undo this right after.' : 'This cannot be undone.'}</span>
              </DialogDescription>
            </>
          ) : (
            <>
              <DialogTitle>{cap(label)}?</DialogTitle>
              <DialogDescription>{text ? cap(text) : 'This cannot be undone.'}</DialogDescription>
            </>
          )}
        </DialogHeader>
        {addon && action && <FromAddon addon={addon} addonTitle={addonTitle ?? addon} label={label} text={text} subject={subject} args={args} />}
        <DialogFooter className="gap-2">
          <Button ref={cancel} variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={onConfirm}>
            {cap(label)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** "Confirm: Stop (stop) · Publish (publish)": core's words only. */
export const confirmTitle = (action: string, addonTitle: string, addon: string) => `Confirm: ${wordsAndId(action)} · ${addonName(addonTitle, addon)}`
