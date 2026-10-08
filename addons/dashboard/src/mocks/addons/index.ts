// Importing this module registers every mock addon.
import './publish'
import './github'
import './usage'
import './terminals'
import './wiki'
import './estimate'
export { actionMinRole, actionRun, getAddon, registerAddon } from './registry'
export type { AddonAction, AddonActionFn, AddonCtx, MockAddon } from './registry'
