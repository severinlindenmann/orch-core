import { hasFeature, isRealHost } from '@/api/features'

export const TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members' },
  { id: 'gates', label: 'Gates' },
  { id: 'relay', label: 'Relay & devices', preview: true },
  { id: 'addons', label: 'Addons' },
  { id: 'skills', label: 'Skills' },
  { id: 'connections', label: 'Connections' },
  { id: 'hub', label: 'Hub' },
] as const

/** The reduced settings of a real host (M4, #306): the rest of the mockup's tabs have no host operation behind them yet. */
export const HOST_TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members' },
  /** Who has to approve each step (the policy of the gates). */
  { id: 'gates', label: 'Approvals' },
  /** The access given to agents (grants). */
  { id: 'grants', label: 'Agent access' },
  /** Switch the shipped add-ons on and off (each change is signed). */
  { id: 'addons', label: 'Add-ons' },
  /** Only while a hub is connected. */
  { id: 'hub', label: 'Hub', feature: 'hub' },
] as const

/**
 * Pages that are still served but are not tabs: the paired devices and the phone pairing live under the Phone add-on
 * (Add-ons), and records are part of General > Advanced. A link to `/settings/records` goes to General.
 */
export const HIDDEN_HOST_TABS = ['devices'] as const

/** The tabs this build shows: the mockup's, or the reduced ones on a real host. */
export const settingsTabs = (workspace?: string): readonly { id: string; label: string; preview?: boolean }[] =>
  isRealHost() ? HOST_TABS.filter((t) => !('feature' in t) || hasFeature(t.feature, workspace)) : TABS

/** The known /settings/$tab values on this build; anything else redirects to General. */
export const knownSettingsTab = (tab: string): boolean => (isRealHost() ? [...HOST_TABS.map((t) => t.id), ...HIDDEN_HOST_TABS] : TABS.map((t) => t.id)).some((id) => id === tab)
