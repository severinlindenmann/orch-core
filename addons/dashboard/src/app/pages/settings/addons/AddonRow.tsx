import { Link } from '@tanstack/react-router'
import { TriangleAlert } from 'lucide-react'
import { AddonBadge } from '@/addon-ui/AddonBadge'
import type { AddonManifest, AddonStatus } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { TableCell, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { CapabilityChips } from './CapabilityChips'

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
}

/** The callout under a row whose installed version has no grant. Warning tone (orange is for addons only). */
export function NeedsGrantNotice({ addon }: { addon: AddonManifest }) {
  const text = addon.granted ? `Updated to ${addon.version}: grant again to turn it back on.` : `Installed ${addon.version}: grant its capabilities to turn it on.`
  return (
    <div role="status" className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-1.5 text-[13px]">
      <TriangleAlert className="size-3.5 shrink-0 text-warning" aria-hidden />
      {text}
    </div>
  )
}

export function AddonRow({ addon, canEdit, hasSettings, actions }: { addon: AddonManifest; canEdit: boolean; hasSettings: boolean; actions: RowActions }) {
  const s = STATUS[addon.status]
  const needsGrant = addon.status === 'needs_grant'
  return (
    <TableRow aria-label={`${addon.title} ${addon.version}`} className="align-top">
      <TableCell className="whitespace-normal">
        <div className="flex items-center gap-2">
          <AddonBadge name={addon.name} />
          <span className="font-medium">{addon.title}</span>
          <span className="font-mono text-[12px] text-text-muted">{addon.version}</span>
        </div>
        <p className="mt-0.5 max-w-xs text-[12px] text-text-muted">{addon.description}</p>
        {needsGrant && (
          <div className="mt-2">
            <NeedsGrantNotice addon={addon} />
          </div>
        )}
      </TableCell>
      <TableCell className="whitespace-normal">
        <CapabilityChips capabilities={addon.capabilities} />
      </TableCell>
      <TableCell>
        <span className="inline-flex items-center gap-1.5 text-[13px]">
          <span className={cn('size-1.5 rounded-full', s.dot)} aria-hidden />
          {s.label}
        </span>
      </TableCell>
      <TableCell>
        <Switch aria-label={`Enable ${addon.title}`} checked={addon.enabled && !needsGrant} disabled={!canEdit || needsGrant} onCheckedChange={actions.setEnabled} />
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap justify-end gap-1.5">
          {canEdit && needsGrant && (
            <Button size="sm" onClick={actions.grant}>
              Grant…
            </Button>
          )}
          {hasSettings && !needsGrant && addon.enabled && (
            <Button size="sm" variant="outline" asChild>
              <Link to="/settings/addon/$name" params={{ name: addon.name }}>
                Settings
              </Link>
            </Button>
          )}
          {canEdit && addon.update && (
            <Button size="sm" variant="outline" onClick={actions.update}>
              Update to {addon.update.version}
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
