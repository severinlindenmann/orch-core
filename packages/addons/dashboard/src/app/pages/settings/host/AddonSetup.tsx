import { useState } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { PublishForm, publishValidation } from './PublishServer'

/** The settings an add-on needs before it can be on (only Publish is built in; other add-ons keep their settings in the generic panel). */
export function AddonSetup({ name, enabling, suggested, suggestedFrom, hubName, description, notice, close, submit }: { name: 'publish'; enabling: boolean; suggested?: string | null; suggestedFrom?: 'computer' | 'hub' | null; hubName?: string | null; description?: string; notice?: string; close: () => void; submit: (title: string, action: HandoffAction) => void }) {
  const title = `${enabling ? 'Turn on' : 'Set up'} Publish`
  // P02: the Publish server installed on this computer is the starting address for files and links; the field hides behind "Change"
  const [server, setServer] = useState(suggested ?? '')
  const [share, setShare] = useState(suggested ?? '')
  const ready = publishValidation(server, share).ready
  const action: HandoffAction = enabling ? { action: 'addon_enable', name, publish: { server, share } } : { action: 'publish_setup', server, share }
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto border-border bg-surface">
        <DialogHeader>
          <DialogTitle><Inline>{title}</Inline></DialogTitle>
          <DialogDescription>{description ?? (enabling ? 'Choose where files go, then turn it on.' : 'Choose where files go, then review and sign.')}</DialogDescription>
        </DialogHeader>
        <PublishForm server={server} share={share} setServer={setServer} setShare={setShare} suggested={suggested} suggestedFrom={suggestedFrom} hubName={hubName} />
        {notice && <p className="rounded-md border border-warning/40 bg-warning-soft px-2.5 py-2 text-[12.5px] text-warning">{notice}</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={close}>Cancel</Button>
          <Button disabled={!ready} onClick={() => submit(title, action)}>{enabling ? 'Turn on and sign' : 'Review and sign'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
