// "Share files…" on a ticket while Publish is off (P05, P04): ONE dialog (the address, prefilled when the Publish server on
// this computer is known); its button goes straight to the host's review with Sign. When Publish is on the share dialog
// follows by itself. The signed change is the same as on the Add-ons page: settings and turn-on, one review, one Sign.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { queries } from '@/api/queries'
import type { Workspace } from '@/api/types'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { BuiltInAddOns } from '../settings/host/AddOns'
import { AddonSetup } from '../settings/host/AddonSetup'
import { useSettingsHandoff } from '../settings/host/useSettingsHandoff'

export function TurnOnPublish({ workspace, onOn, onClose }: { workspace: Workspace; onOn: () => void; onClose: () => void }) {
  const rows = useQuery(queries.addOns(workspace.id))
  const qc = useQueryClient()
  const row = rows.data?.available ? rows.data.add_ons.find((r) => r.id === 'publish') : undefined
  const on = row?.state === 'on'
  // The address dialog is shown until the person presses Turn on and sign; then the review replaces it (never both).
  const [asked, setAsked] = useState(false)
  const { ask, dialog } = useSettingsHandoff(workspace.id, async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['add-ons', workspace.id] }),
      qc.invalidateQueries({ queryKey: ['workspaces'] }),
      qc.invalidateQueries({ queryKey: ['workspace-addons', workspace.id] }),
    ])
  })
  useEffect(() => {
    if (on) onOn()
  }, [on, onOn])
  // Needs its address first (the same rule as the Add-ons page): one dialog with the address, then the review.
  const needsAddress = !!row && row.state !== 'on' && row.locked === null && !!row.needs && row.set_up !== true
  // Latched once the person pressed the button: when the settings step lands the row reads set_up, but the review must stay
  // mounted until both steps are signed (otherwise the list dialog returns and the review unmounts between the steps).
  if ((needsAddress || asked) && row)
    return (
      <>
        {!asked && (
          <AddonSetup
            name="publish"
            enabling
            suggested={row.suggest}
            suggestedFrom={row.suggest_from}
            hubName={row.suggest_hub}
            description="To share files with a link, Publish has to be on."
            notice="Anyone with the link can read what you share."
            close={onClose}
            submit={(title, action) => {
              setAsked(true)
              ask({ title, action, prepareOnOpen: true, onEnd: (signed) => { if (!signed) onClose() } })
            }}
          />
        )}
        {dialog}
      </>
    )
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
