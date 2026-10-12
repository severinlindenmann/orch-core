import { isRealHost } from '@/api/features'

export const TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members' },
  { id: 'gates', label: 'Gates' },
  { id: 'relay', label: 'Relay & devices', preview: true },
  { id: 'addons', label: 'Addons' },
  { id: 'skills', label: 'Skills' },
  { id: 'connections', label: 'Connections' },
] as const

/** The reduced settings of a real host (M4, #306): the rest of the mockup's tabs have no host operation behind them yet. */
export const HOST_TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members' },
  { id: 'gates', label: 'Gates' },
  { id: 'grants', label: 'Grants' },
] as const

/** The tabs this build shows: the mockup's, or the reduced ones on a real host. */
export const settingsTabs = (): readonly { id: string; label: string; preview?: boolean }[] => (isRealHost() ? HOST_TABS : TABS)

/** The known /settings/$tab values on this build; anything else redirects to General. */
export const knownSettingsTab = (tab: string): boolean => settingsTabs().some((t) => t.id === tab)
