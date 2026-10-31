// "Share files…" on a ticket while Publish is off (P05, P04): ONE dialog (the address, prefilled when the Publish server on
// this computer is known); its button goes straight to the host's review with Sign. When Publish is on the share dialog
// follows by itself. The signed change is the same as on the Add-ons page: settings and turn-on, one review, one Sign.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { queries } from '@/api/queries'
import { ReservedLead, reviewFacts } from './reviewFacts'
import { destinationLine } from '@/api/shares'
import type { Workspace } from '@/api/types'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { BuiltInAddOns } from '../settings/host/AddOns'
import { AddonSetup } from '../settings/host/AddonSetup'
import { publishValidation } from '../settings/host/PublishServer'
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
  // The address is already known (the Publish server on this computer, or the hub's): no address dialog. The person's press
  // that opened this already said what happens; the one review follows at once, prepared only (never armed): Sign stays
  // their own press. Decided ONCE, on the first load of the row, so a suggestion that shows up later never replaces a form
  // the person is typing in. Without a usable suggestion the address form below stays.
  const latch = useRef<string | false | null>(null)
  if (row && latch.current === null) latch.current = needsAddress && row.suggest && publishValidation(row.suggest, row.suggest).ready ? row.suggest : false
  const suggested = latch.current || null
  useEffect(() => {
    if (!suggested || asked || !row) return
    setAsked(true)
    const where = destinationLine(row.suggest_from, row.suggest_hub, suggested)
    ask({
      title: 'Turn on Publish',
      doneToast: '',
      confirmLabel: 'Sign and turn on',
      action: { action: 'addon_enable', name: 'publish', publish: { server: suggested, share: suggested } },
      prepareOnOpen: true,
      lead: (content) => <ReservedLead>{!reviewFacts(content).destination && where ? <p data-testid="where-files-go" className="text-[13px] font-medium text-text">{where}</p> : null}</ReservedLead>,
      onEnd: (signed) => { if (!signed) onClose() },
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suggested, asked, !!row])
  // Latched once the person pressed the button: when the settings step lands the row reads set_up, but the review must stay
  // mounted until both steps are signed (otherwise the list dialog returns and the review unmounts between the steps).
  if ((needsAddress || asked) && row)
    return (
      <>
        {!asked && !suggested && (
          <AddonSetup
            name="publish"
            enabling
            suggested={row.suggest}
            suggestedFrom={row.suggest_from}
            hubName={row.suggest_hub}
            description="To share files with a link, Publish has to be on."
            close={onClose}
            submit={(title, action) => {
              setAsked(true)
              ask({ title, action, doneToast: '', prepareOnOpen: true, onEnd: (signed) => { if (!signed) onClose() } })
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
        <BuiltInAddOns quiet workspace={workspace} browse only={['publish']} label="Publish" />
      </DialogContent>
    </Dialog>
  )
}
