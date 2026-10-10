import { ChevronDown, TriangleAlert } from 'lucide-react'
import { useEffect, useRef } from 'react'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import { PreviewChip } from '@/addon-ui/PreviewChip'
import { pendingUpdate } from '@/api/addons'
import type { AddonStatus, InstalledAddon } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { TableCell, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { CapabilityChips } from './CapabilityChips'
import { takeSettingsFocus } from './AddonSettingsPanel'

const STATUS: Record<AddonStatus, { label: string; dot: string }> = {
  active: { label: 'Active', dot: 'bg-success' },
  disabled: { label: 'Disabled', dot: 'bg-text-faint' },
  needs_grant: { label: 'Needs grant', dot: 'bg-warning' },
}

export interface RowActions {
  grant: () => void
  update: () => void
  uninstall: () => void
  setEnabled: (on: boolean) => void
  openSettings: () => void
}

/** The callout under a row whose installed version has no grant. Warning tone (orange is for addons only). */
export function NeedsGrantNotice({ addon }: { addon: InstalledAddon }) {
  const { granted, version } = addon.ws
  const text = granted ? `Updated to ${version}: grant again to turn it back on.` : `Installed ${version}: grant its capabilities to turn it on.`
  return (
    <div role="status" className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-1.5 text-[13px]">
      <TriangleAlert className="size-3.5 shrink-0 text-warning" aria-hidden />
      {text}
    </div>
  )
}

/** One installed addon: everything about this workspace's install comes from `addon.ws`. */
export function AddonRow({ addon, active, canEdit, hasSettings, settingsOpen, actions }: { addon: InstalledAddon; settingsOpen: boolean; active: boolean; canEdit: boolean; hasSettings: boolean; actions: RowActions }) {
  const s = STATUS[addon.ws.status]
  const needsGrant = addon.ws.status === 'needs_grant'
  const update = pendingUpdate(addon)
  const settingsButton = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (takeSettingsFocus(addon.name)) settingsButton.current?.focus()
  }, [addon.name])
  return (
    <TableRow aria-label={`${addon.title} ${addon.ws.version}`} className="align-top">
      <TableCell className="whitespace-normal">
        <div className="flex items-center gap-2">
          <AddonBadge name={addon.name} />
          <span className="font-medium">{addon.title}</span>
          <PreviewChip name={addon.name} />
          <span className="font-mono text-[12px] text-text-muted">{addon.ws.version}</span>
        </div>
        <p className="mt-0.5 max-w-xs text-[12px] text-text-muted">{addon.description}</p>
        {needsGrant && (
          <div className="mt-2">
            <NeedsGrantNotice addon={addon} />
          </div>
        )}
      </TableCell>
      <TableCell className="whitespace-normal">
        <CapabilityChips capabilities={addon.ws.capabilities} />
      </TableCell>
      <TableCell>
        <span className="inline-flex items-center gap-1.5 text-[13px]">
          <span className={cn('size-1.5 rounded-full', s.dot)} aria-hidden />
          {s.label}
        </span>
      </TableCell>
      <TableCell>
        <Switch aria-label={`${addon.title} ${addon.ws.enabled && !needsGrant ? 'enabled' : 'disabled'}`} checked={addon.ws.enabled && !needsGrant} disabled={!canEdit || needsGrant} onCheckedChange={actions.setEnabled} />
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap justify-end gap-1.5">
          {canEdit && needsGrant && (
            <Button size="sm" onClick={actions.grant}>
              Grant…
            </Button>
          )}
          {hasSettings && active && (
            <Button ref={settingsButton} size="sm" variant="outline" data-settings-for={addon.name} aria-expanded={settingsOpen} aria-controls={settingsOpen ? `addon-settings-panel-${addon.name}` : undefined} onClick={actions.openSettings}>
              Settings
              <ChevronDown className={cn('transition-transform', settingsOpen && 'rotate-180')} aria-hidden />
            </Button>
          )}
          {canEdit && update && (
            <Button size="sm" variant="outline" onClick={actions.update}>
              Update to {update.version}
            </Button>
          )}
          {canEdit && (
            <Button size="sm" variant="ghost" onClick={actions.uninstall}>
              Uninstall
            </Button>
          )}
        </div>
      </TableCell>
    </TableRow>
  )
}
