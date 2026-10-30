// "Share files…" on a ticket while Publish is off (P05): the same turn-on review and signature as the Add-ons page, in a
// small dialog, and when Publish is on the share dialog follows by itself.
import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { queries } from '@/api/queries'
import type { Workspace } from '@/api/types'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { BuiltInAddOns } from '../settings/host/AddOns'

export function TurnOnPublish({ workspace, onOn, onClose }: { workspace: Workspace; onOn: () => void; onClose: () => void }) {
  const rows = useQuery(queries.addOns(workspace.id))
  const on = rows.data?.available && rows.data.add_ons.some((r) => r.id === 'publish' && r.state === 'on')
  useEffect(() => {
    if (on) onOn()
  }, [on, onOn])
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90vh] max-w-md gap-3 overflow-y-auto border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Turn on Publish</DialogTitle>
          <DialogDescription>To share files with a link, Publish has to be on. Nothing leaves this computer until you sign.</DialogDescription>
        </DialogHeader>
        <BuiltInAddOns workspace={workspace} browse only={['publish']} label="Publish" />
      </DialogContent>
    </Dialog>
  )
}
