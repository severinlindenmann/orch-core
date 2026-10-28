import { useState } from 'react'
import type { HandoffAction } from '@/api/handoff'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { PublishForm, publishValidation } from './PublishServer'

/** The settings an add-on needs before it can be on (only Publish is built in; other add-ons keep their settings in the generic panel). */
export function AddonSetup({ name, enabling, close, submit }: { name: 'publish'; enabling: boolean; close: () => void; submit: (title: string, action: HandoffAction) => void }) {
  const title = `${enabling ? 'Turn on' : 'Set up'} Publish`
  const [server, setServer] = useState('')
  const [share, setShare] = useState('')
  const ready = publishValidation(server, share).ready
  const action: HandoffAction = enabling ? { action: 'addon_enable', name, publish: { server, share } } : { action: 'publish_setup', server, share }
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto border-border bg-surface">
        <DialogHeader>
          <DialogTitle><Inline>{title}</Inline></DialogTitle>
          <DialogDescription>{enabling ? 'Choose the settings, then review and sign to turn it on.' : 'Choose the settings, then review and sign.'}</DialogDescription>
        </DialogHeader>
        <PublishForm server={server} share={share} setServer={setServer} setShare={setShare} />
        <DialogFooter>
          <Button variant="ghost" onClick={close}>Cancel</Button>
          <Button disabled={!ready} onClick={() => submit(title, action)}>{enabling ? 'Turn on' : 'Review and sign'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
