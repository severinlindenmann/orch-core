// Importing this module registers every mock addon.
import './publish'
import './github'
import './usage'
import './terminals'
import './wiki'
import './estimate'
export { getAddon, registerAddon } from './registry'
export type { AddonCtx, MockAddon } from './registry'
