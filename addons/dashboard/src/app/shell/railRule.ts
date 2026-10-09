// Wide sidebar or icon rail (N11). Two remembered choices: the usual one, and the one for when the terminal dock on
// the right squeezes the page (page area under RAIL_SQUEEZE px with the wide sidebar). Without a choice the sidebar
// follows the window (rail below 1280 px) and, while the dock squeezes the page, is the rail. A choice made while the
// dock squeezes is kept for that situation only, so expanding the sidebar beside the dock does not pin it open
// everywhere else, and collapsing it on a wide screen does not decide what happens beside the dock.

export type RailPref = 'auto' | 'wide' | 'narrow'

/** The sidebar's widths (px). */
export const SIDEBAR_WIDE = 232
export const SIDEBAR_RAIL = 56
/** Below this window width the sidebar is the rail by default. */
export const RAIL_WINDOW = 1280
/** With the dock on the right, a page area (beside the wide sidebar) under this makes the sidebar the rail by default. */
export const RAIL_SQUEEZE = 900

export interface RailInputs {
  /** The usual choice. */
  pref: RailPref
  /** The choice for when the dock squeezes the page. */
  dockPref: RailPref
  windowWidth: number
  /** The right-hand dock squeezes the page (see `dockSqueezesSidebar` in the dock prefs). */
  squeezed: boolean
}

/** Is the sidebar the icon rail? */
export function railCollapsed({ pref, dockPref, windowWidth, squeezed }: RailInputs): boolean {
  if (squeezed) return dockPref !== 'wide'
  return pref === 'auto' ? windowWidth < RAIL_WINDOW : pref === 'narrow'
}

/** Which choice a toggle records (the situation it is made in) and its value. */
export function railToggle(inputs: RailInputs): { which: 'pref' | 'dockPref'; value: RailPref } {
  return { which: inputs.squeezed ? 'dockPref' : 'pref', value: railCollapsed(inputs) ? 'wide' : 'narrow' }
}
