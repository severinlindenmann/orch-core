import { hasFeature, isRealHost } from '@/api/features'

export const TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members' },
  { id: 'gates', label: 'Gates' },
  { id: 'profile', label: 'Profile' },
  { id: 'relay', label: 'Relay & devices', preview: true },
  { id: 'addons', label: 'Addons' },
  { id: 'skills', label: 'Skills' },
  { id: 'connections', label: 'Connections' },
  { id: 'hub', label: 'Hub' },
  { id: 'company', label: 'Company' },
] as const

/** The reduced settings of a real host (M4, #306): the rest of the mockup's tabs have no host operation behind them yet. */
export const HOST_TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members' },
  /** Who has to approve each step (the policy of the gates). */
  { id: 'gates', label: 'Approvals' },
  /** The access given to agents (grants). */
  { id: 'grants', label: 'Agent access' },
  /** Phone pairing and the devices that can approve. */
  { id: 'devices', label: 'Devices' },
  /** What this workspace does, for the hub's directory. Works without a hub. */
  { id: 'profile', label: 'Profile' },
  /** Only while a hub is connected. */
  { id: 'hub', label: 'Hub', feature: 'hub' },
  /** Company skills and tools the hub offers: the same condition as the Hub tab. */
  { id: 'company', label: 'Company', feature: 'hub' },
] as const

/** The tabs this build shows: the mockup's, or the reduced ones on a real host. */
export const settingsTabs = (workspace?: string): readonly { id: string; label: string; preview?: boolean }[] =>
  isRealHost() ? HOST_TABS.filter((t) => !('feature' in t) || hasFeature(t.feature, workspace)) : TABS

/** The known /settings/$tab values on this build; anything else (also the old /settings/devices and /settings/records) redirects to General. Devices and phone pairing live in Settings > Devices, add-ons on the Add-ons page, records in General > Advanced. */
export const knownSettingsTab = (tab: string): boolean => (isRealHost() ? HOST_TABS.map((t) => t.id) : TABS.map((t) => t.id)).some((id) => id === tab)
