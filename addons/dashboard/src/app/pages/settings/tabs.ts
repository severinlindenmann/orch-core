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
  { id: 'gates', label: 'Gates' },
  { id: 'grants', label: 'Grants' },
  /** Switch the shipped add-ons on and off (each change is signed). */
  { id: 'addons', label: 'Add-ons' },
  /** Only where the host serves `records/1` (M6). */
  { id: 'records', label: 'Records', feature: 'records' },
  /** Pair a phone and the paired devices (proposed `pair/1`). Always listed: without the ops it says "not available". */
  { id: 'devices', label: 'Devices' },
  { id: 'hub', label: 'Hub', feature: 'hub' },
] as const

/** The tabs this build shows: the mockup's, or the reduced ones on a real host. */
export const settingsTabs = (workspace?: string): readonly { id: string; label: string; preview?: boolean }[] =>
  isRealHost() ? HOST_TABS.filter((t) => !('feature' in t) || hasFeature(t.feature, workspace)) : TABS

/** The known /settings/$tab values on this build; anything else redirects to General. */
export const knownSettingsTab = (tab: string): boolean => (isRealHost() ? HOST_TABS : TABS).some((t) => t.id === tab)
