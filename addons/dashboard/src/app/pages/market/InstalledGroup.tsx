// "From marketplaces" on the Installed tab: the add-ons installed from a marketplace, in the same rows as the built-in
// ones (switch under the same inline accordion). Install lands them Off; the row switch turns them on.
import { Button } from '@/components/ui/button'
import { stateLabel, type InstalledAddon } from '@/api/market'
import type { AddOnRow } from '@/api/addOnSwitches'
import type { Workspace } from '@/api/types'
import { BuiltInAddOns } from '../settings/host/AddOns'

/** An installed marketplace add-on as a switch row: active is On, everything else Off (the chip says more). */
export function asRow(a: InstalledAddon, summary: string | undefined): AddOnRow {
  return {
    id: a.name,
    title: a.name,
    desc: summary ?? `Version ${a.version}`,
    state: a.state === 'active' ? 'on' : 'off',
    locked: null,
    blocked: null,
    version: a.version,
  }
}

export function InstalledGroup({
  workspace,
  addons,
  summaryOf,
  mayWrite,
  busy,
  onUpdate,
  onSettings,
  onRemove,
}: {
  workspace: Workspace
  addons: InstalledAddon[]
  summaryOf: (name: string) => string | undefined
  mayWrite: boolean
  busy: boolean
  onUpdate: (a: InstalledAddon) => void
  onSettings?: (a: InstalledAddon) => void
  onRemove: (a: InstalledAddon) => void
}) {
  const by = new Map(addons.map((a) => [a.name, a]))
  return (
    <BuiltInAddOns
      workspace={workspace}
      rows={addons.map((a) => asRow(a, summaryOf(a.name)))}
      tag="Marketplace"
      label="From marketplaces"
      empty="Nothing installed from a marketplace yet. Browse has what is on offer."
      actions={(row) => {
        const a = by.get(row.id)
        if (!a) return null
        if (!mayWrite) return <span className="text-[11px] text-text-faint">{a.version}</span>
        return (
          <div className="flex flex-wrap items-center gap-1">
            <span className="text-[11px] text-text-faint">{a.version}</span>
            {a.state !== 'active' && <span className="text-[11px] text-text-faint">{stateLabel(a.state)}</span>}
            {a.available && !a.available.revoked && (
              <Button size="sm" variant="secondary" disabled={busy} onClick={() => onUpdate(a)}>
                Update...
              </Button>
            )}
            {onSettings && (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => onSettings(a)}>
                Settings
              </Button>
            )}
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => onRemove(a)}>
              Remove
            </Button>
          </div>
        )
      }}
    />
  )
}
