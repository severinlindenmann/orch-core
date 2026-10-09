import { Check, Copy, KeyRound } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { AddonBadge } from './AddonBadge'

/**
 * Core's modal for a value shown once (a share link): the value in a read-only field, Copy, and "I saved it". It does
 * not close on an outside click or Escape and has no X, so the link cannot be lost by accident; only "I saved it" closes it.
 * The addon's words (label, note) are plain text; the title is core's.
 */
export function SecretDialog({ addon, secret, onDone }: { addon: string; secret: { label: string; value: string; note?: string }; onDone: () => void }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    void navigator.clipboard?.writeText(secret.value).then(
      () => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      },
      () => {},
    )
  }
  return (
    <Dialog open>
      <DialogContent
        showCloseButton={false}
        className="max-w-lg gap-4 border-border bg-surface"
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
        onOpenAutoFocus={(e) => {
          e.preventDefault()
          ;(e.currentTarget as HTMLElement | null)?.querySelector<HTMLInputElement>('input')?.select()
        }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRound className="size-4 text-brand" />
            Copy this link now
          </DialogTitle>
          <DialogDescription className="flex items-center gap-1.5">
            <AddonBadge name={addon} />
            Copy it before you close this window.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <label htmlFor="secret-value" className="text-[13px] text-text-muted">
            {secret.label}
          </label>
          <div className="flex items-center gap-2">
            <Input id="secret-value" readOnly value={secret.value} className="font-mono text-[12px]" onFocus={(e) => e.currentTarget.select()} />
            <Button type="button" variant="outline" onClick={copy}>
              {copied ? <Check /> : <Copy />}
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
          {secret.note && <p className="break-words text-[12px] text-text-muted">{secret.note}</p>}
        </div>
        <DialogFooter>
          <Button onClick={onDone}>I saved it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
