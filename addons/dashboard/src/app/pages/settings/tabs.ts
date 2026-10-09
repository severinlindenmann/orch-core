export const TABS = [
  { id: 'general', label: 'General' },
  { id: 'members', label: 'Members' },
  { id: 'gates', label: 'Gates' },
  { id: 'addons', label: 'Addons' },
] as const

/** The known /settings/$tab values; anything else redirects to General. */
export const SETTINGS_TABS: readonly string[] = TABS.map((t) => t.id)
