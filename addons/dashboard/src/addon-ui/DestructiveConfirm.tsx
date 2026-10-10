import { useRef } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { addonName, argLines, FromAddon, wordsAndId } from './SignConfirm'
import { SentArgs } from './SentArgs'
import { coreNote } from './coreNotes'

const MAX = 160
const cap = (v: string) => (v.length > MAX ? `${v.slice(0, MAX)}…` : v)

/**
 * Core's confirm for an action the manifest marks `confirm: 'destructive'`. It is not a signature. Trust split (as
 * SignConfirm): the title and the lines above the region are core's words (the action id, the addon's name, the
 * ticket, every arg that is sent, the consequence); the package's words (label, sentence, the row's name) sit in the
 * dashed "From the addon" region. The button is core's ("Confirm: Revoke (revoke)"), not "OK"; Cancel has focus.
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
  /** The manifest's undo target is a plain action this viewer may run (core's static check): core says the addon offers an undo. */
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
                <span data-testid="consequence">{undoable ? 'The addon offers an undo right after.' : 'This cannot be undone.'}</span>
              </DialogDescription>
            </>
          ) : (
            <>
              <DialogTitle>{cap(label)}?</DialogTitle>
              <DialogDescription>{text ? cap(text) : 'This cannot be undone.'}</DialogDescription>
            </>
          )}
        </DialogHeader>
        {addon && action && (
          <>
            {coreNote(addon, action) && <p className="text-[13px]" data-testid="core-note">{coreNote(addon, action)}</p>}
            <SentArgs lines={argLines(args)} />
            <FromAddon addon={addon} addonTitle={addonTitle ?? addon} label={label} text={text} subject={subject} />
          </>
        )}
        <DialogFooter className="gap-2">
          <Button ref={cancel} variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          {/* Core's only confirm control: core's words for the action, never the addon's label (that is in its region). */}
          <Button variant="destructive" onClick={onConfirm}>
            {addon && action ? `Confirm: ${wordsAndId(action)}` : cap(label)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** "Confirm: Stop (stop) · Publish (publish)": core's words only. */
export const confirmTitle = (action: string, addonTitle: string, addon: string) => `Confirm: ${wordsAndId(action)} · ${addonName(addonTitle, addon)}`
