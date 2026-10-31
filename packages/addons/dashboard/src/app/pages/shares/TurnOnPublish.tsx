// "Share files…" on a ticket while Publish is off (P05, P04): ONE dialog (the address, prefilled when the Publish server on
// this computer is known); its button goes straight to the host's review with Sign. When Publish is on the share dialog
// follows by itself. The signed change is the same as on the Add-ons page: settings and turn-on, one review, one Sign.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { queries } from '@/api/queries'
import { suggestedWhere, type AddOnRow, type PublishWhere } from '@/api/addOnSwitches'
import { destinationLine, hostOf, sameOrigin } from '@/api/shares'
import type { Workspace } from '@/api/types'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { BuiltInAddOns } from '../settings/host/AddOns'
import { AddonSetup } from '../settings/host/AddonSetup'
import { publishValidation } from '../settings/host/PublishServer'
import { useSettingsHandoff, type HandoffSpec } from '../settings/host/useSettingsHandoff'

/**
 * The ONE turn-on review, pre-filled from the server's row only: both values (`server` and `share`, kept apart) go into the
 * signed settings, and the lead names where files go (by name) and, when it is another host, where links open. Prepared
 * on open, never armed: Sign stays the person's own press. Used by this dialog and by Settings, Hub.
 */
export function turnOnSpec(where: PublishWhere, row: Pick<AddOnRow, 'suggest_from' | 'suggest_hub'>, onEnd: (signed: boolean) => void): HandoffSpec {
  const line = destinationLine(row.suggest_from, row.suggest_hub, where.server)
  const links = !sameOrigin(where.server, where.share)
  return {
    title: 'Turn on Publish',
    confirmLabel: 'Sign and turn on',
    action: { action: 'addon_enable', name: 'publish', publish: { server: where.server, share: where.share } },
    prepareOnOpen: true,
    lead: line ? (
      <>
        <p data-testid="where-files-go" className="text-[13px] font-medium text-text">{line}</p>
        {links && <p data-testid="links-open-at" className="text-[13px] text-text">Links open at {hostOf(where.share)}.</p>}
      </>
    ) : undefined,
    onEnd,
  }
}

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
  const latch = useRef<PublishWhere | false | null>(null)
  if (row && latch.current === null) {
    // Both values come from the row: `suggest_where` first, the one-string `suggest` only when it is missing.
    const where = suggestedWhere(row)
    latch.current = needsAddress && where && publishValidation(where.server, where.share).ready ? where : false
  }
  const suggested = latch.current || null
  useEffect(() => {
    if (!suggested || asked || !row) return
    setAsked(true)
    ask(turnOnSpec(suggested, row, (signed) => { if (!signed) onClose() }))
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
            suggested={suggestedWhere(row)}
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
