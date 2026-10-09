import { useRef } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

const MAX = 160
const cap = (v: string) => (v.length > MAX ? `${v.slice(0, MAX)}…` : v)

/**
 * Core's confirm for an action the manifest marks `confirm: 'destructive'`. The button names the consequence
 * ("Revoke link", "Remove worktree"), not "OK"; Cancel is the default focus. The package supplies the button text and
 * one sentence (plain text, capped); the title and the structure are core's. It is not a signature.
 */
export function DestructiveConfirm({ label, text, subject, onConfirm, onClose }: { label: string; text?: string; subject?: string; onConfirm: () => void; onClose: () => void }) {
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
          <DialogTitle>{cap(label)}{subject ? `: ${cap(subject)}` : ''}?</DialogTitle>
          <DialogDescription>{text ? cap(text) : 'This cannot be undone.'}</DialogDescription>
        </DialogHeader>
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
