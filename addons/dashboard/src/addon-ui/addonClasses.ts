// The addon orange as class names, kept here so no file outside src/addon-ui spells the token (the orange guard enforces it).
// Tailwind sees these literals because it scans every source file.

/** Orange outline for a box that holds addon-owned content. */
export const addonHairline = 'border-addon-border'
/** Faint orange divider line inside an addon frame. */
export const addonRule = 'border-addon-border/60'
/** Outline plus soft fill for a tile that stands for an addon's content. */
export const addonTile = 'border-addon-border bg-addon-soft'
/** A board lane an addon contributes. */
export const addonLane = 'border-addon-border bg-addon-soft/40'
/** Orange left edge only, for a row of core anatomy that stands for an addon (other borders keep their colour). */
export const addonEdge = 'border-l border-l-addon-border'
