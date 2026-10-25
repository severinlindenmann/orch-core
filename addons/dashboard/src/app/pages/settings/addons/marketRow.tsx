import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { StateBadge, StateChip } from '@/addon-ui/StateChip'
import type { InstalledAddon as MarketInstalled } from '@/api/market'
import { queries } from '@/api/queries'
import { Button } from '@/components/ui/button'
import { takeSettingsFocus } from './AddonSettingsPanel'

/** The one reading of the host's install state: On is `active` (or `enabled`), everything else installed is Off. */
export const isOn = (state?: string) => state === 'active' || state === 'enabled'

/** Names of the add-ons whose installed version has no grant yet (the same source as the built-in rows' "Needs your approval"). */
export function useNeedsApproval(ws: string): Set<string> {
  const addons = useQuery(queries.workspaceAddons(ws))
  return new Set((addons.data ?? []).filter((a) => a.ws.status === 'needs_grant').map((a) => a.name))
}

/** The On/Off chip with its separate badges, the same in the Installed tab and in Settings > Add-ons. */
export function MarketStateChips({ addon, needsApproval }: { addon: MarketInstalled; needsApproval: boolean }) {
  return (
    <>
      <StateChip on={isOn(addon.state) && !needsApproval} />
      {needsApproval && <StateBadge kind="approval" />}
      {addon.available && !addon.available.revoked && <StateBadge kind="update" />}
    </>
  )
}

/**
 * The Settings button of a marketplace add-on, one component for both pages (the only place `data-settings-for` is set for
 * these rows). When the list is drawn again by a route change it takes the keyboard back, as a built-in row's button does.
 */
export function MarketSettingsButton({ name, open, onToggle, className }: { name: string; open: boolean; onToggle: () => void; className?: string }) {
  const button = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (takeSettingsFocus(name)) button.current?.focus()
  }, [name])
  return (
    <Button ref={button} size="sm" variant="outline" className={className} data-settings-for={name} aria-expanded={open} aria-controls={open ? `addon-settings-panel-${name}` : undefined} onClick={onToggle}>
      Settings
    </Button>
  )
}
