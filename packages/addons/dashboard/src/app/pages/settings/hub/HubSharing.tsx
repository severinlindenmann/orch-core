// Sharing links through the hub (#1838). The Publish add-on row from the dashboard server carries what the hub SIGNED and core
// verified: where files go and where links open (`suggest_where`), and, when the hub has moved since, its new values
// (`hub_moved`). Nothing here reads a URL or local state for those values: the pre-fill is the row, and the signed change is
// still the person's own Sign in the one review.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { suggestedWhere, type AddOnRow, type PublishWhere } from '@/api/addOnSwitches'
import { queries } from '@/api/queries'
import { hostOf, hubMovedText, sameOrigin, shareQueries } from '@/api/shares'
import { canChangeSettings } from '@/api/settings'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { OwnerNote } from '../OwnerNote'
import { publishValidation } from '../host/PublishServer'
import { useSettingsHandoff, type HandoffSpec } from '../host/useSettingsHandoff'
import { useViewerRole } from '../host/useViewerRole'
import { turnOnSpec } from '../../shares/TurnOnPublish'
import { Panel } from './shared'

/** The Publish row and the one review: both open from the server's row only. */
function useHubSharing(workspace: Workspace) {
  const ws = workspace.id
  const qc = useQueryClient()
  const rows = useQuery(queries.addOns(ws))
  const row: AddOnRow | undefined = rows.data?.available ? rows.data.add_ons.find((r) => r.id === 'publish') : undefined
  const owner = canChangeSettings(useViewerRole(workspace))
  const { ask, dialog } = useSettingsHandoff(ws, async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['add-ons', ws] }),
      qc.invalidateQueries({ queryKey: ['workspaces'] }),
      qc.invalidateQueries({ queryKey: ['workspace-addons', ws] }),
      qc.invalidateQueries({ queryKey: ['shares', ws] }),
    ])
  })
  return { row, owner, ask, dialog }
}

const usable = (w: PublishWhere | null | undefined): w is PublishWhere => !!w && publishValidation(w.server, w.share).ready

/**
 * Settings > Hub, joined: one plain line and one button while the hub offers where to share and Publish is not on; or, when
 * the hub has moved, the paused line. The review (`dialog`) is always mounted here, so the row changing after the signature
 * (the line goes away) never unmounts the review before its done step.
 */
export function HubSharing({ workspace }: { workspace: Workspace }) {
  const { row, owner, ask, dialog } = useHubSharing(workspace)
  const current = useQuery(shareQueries.list(workspace.id))
  const where = suggestedWhere(row)
  const offer = !!row && !row.hub_moved && row.suggest_from === 'hub' && row.state !== 'on' && usable(where)
  // The person's own settings, when the row says they are set up: signing replaces them, so the review says so. Their values
  // come from the Shares read; until it has answered the button waits (it must not offer to replace what it cannot name).
  const own = row?.set_up === true
  const configured = current.data && current.data.available && current.data.configured ? { server: current.data.server, share: current.data.share } : null
  const reading = own && current.isPending
  return (
    <>
      {row?.hub_moved ? (
        <Panel title="Sharing paused" tone="border-warning/40 bg-warning-soft">
          <Paused workspace={workspace} row={row} owner={owner} ask={ask} />
        </Panel>
      ) : offer ? (
        <Panel title="Share links">
          <p data-testid="hub-share-line">Share links through {hostOf(where!.share)}</p>
          {owner ? (
            <Button data-testid="hub-share-turn-on" variant="outline" className="h-auto max-w-full whitespace-normal text-left" disabled={reading} onClick={() => ask(turnOnSpec(where!, row!, () => {}, own ? configured ?? { server: null, share: null } : null))}>
              Turn on sharing
            </Button>
          ) : (
            <OwnerNote>Only owners can change this.</OwnerNote>
          )}
        </Panel>
      ) : null}
      {dialog}
    </>
  )
}

/** The Shares page: the paused line and its button, when the hub has moved. Nothing when it has not. */
export function SharingPaused({ workspace }: { workspace: Workspace }) {
  const { row, owner, ask, dialog } = useHubSharing(workspace)
  return (
    <>
      {row?.hub_moved && (
        <div data-testid="shares-hub-moved" className="space-y-2 rounded-lg border border-warning/40 px-3 py-2">
          <Paused workspace={workspace} row={row} owner={owner} ask={ask} />
        </div>
      )}
      {dialog}
    </>
  )
}

/**
 * The hub's address moved: the paused line, and one button that opens the review with both destinations: where files go and
 * where links open, each with the old host when it differs. Both values are signed, so both are shown.
 */
function Paused({ workspace, row, owner, ask }: { workspace: Workspace; row: AddOnRow; owner: boolean; ask: (spec: HandoffSpec) => void }) {
  const current = useQuery(shareQueries.list(workspace.id))
  const moved = row.hub_moved
  if (!moved) return null
  // The pair in force now (this workspace's own settings), when the host says it.
  const old = current.data && current.data.available && current.data.configured ? { server: current.data.server, share: current.data.share } : null
  const oldServer = old?.server && !sameOrigin(old.server, moved.server) && hostOf(old.server) !== hostOf(moved.server) ? hostOf(old.server) : null
  const oldShare = old?.share && !sameOrigin(old.share, moved.share) && hostOf(old.share) !== hostOf(moved.share) ? hostOf(old.share) : null
  const open = () =>
    ask({
      title: 'Use the hub’s new sharing address',
      confirmLabel: 'Sign and use the new address',
      action: { action: 'publish_setup', server: moved.server, share: moved.share },
      prepareOnOpen: true,
      lead: (
        <>
          <p data-testid="hub-moved-files" className="text-[13px] font-medium text-text">Files go to {hostOf(moved.server)}.</p>
          {oldServer && <p data-testid="hub-moved-files-old" className="text-[13px] text-text">Until now they went to {oldServer}.</p>}
          <p data-testid="hub-moved-new" className="text-[13px] font-medium text-text">Links open at {hostOf(moved.share)}.</p>
          {oldShare && <p data-testid="hub-moved-old" className="text-[13px] text-text">Until now they opened at {oldShare}. Links already made keep their old address.</p>}
        </>
      ),
    })
  return (
    <>
      <p role="status" data-testid="hub-moved-line" className="text-[13px]">{hubMovedText(moved, old)}</p>
      {owner ? (
        <Button data-testid="hub-use-new-address" size="sm" variant="secondary" onClick={open} disabled={!usable(moved)}>
          Use the new address
        </Button>
      ) : (
        <OwnerNote>Only owners can change this.</OwnerNote>
      )}
    </>
  )
}
