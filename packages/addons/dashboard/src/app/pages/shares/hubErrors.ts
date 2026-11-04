// Plain words for the hub refusals of a publish (run-14 P01). The host's own text is never echoed (it could carry text
// from elsewhere): each code has its sentence here. Everything else falls through to the shares page's own words.
import { shareError } from '@/api/shares'
import { ApiError } from '@/api/types'

const HUB_TEXT: Record<string, string> = {
  'hub.not_joined': 'Publishing goes through a hub. Connect this workspace to a hub in Settings, Hub, then try again. Nothing was shared.',
  'hub.state_mismatch': 'The hub connection on this computer does not match what the workspace has signed. Open Settings, Hub and connect again, then try again. Nothing was shared.',
  'hub.unreachable': 'The hub cannot be reached right now. Nothing was shared. Try again in a moment.',
}

// The host's one sentence for a file that is too big for the hub link ("Files over 431.5 KB can't go through the hub yet.
// (name is 2.0 MB.)"). The limit comes from core, so the page shows the host's words, but only text of exactly this shape:
// a size, a file name from the server's alphabet and nothing else. Anything else falls back to the fixed sentence.
const SIZE = '[0-9]{1,6}(?:\\.[0-9])? (?:bytes|KB|MB)'
const HUB_TOO_BIG = new RegExp(`^Files over ${SIZE} can't go through the hub yet\\.(?: \\([A-Za-z0-9 ._-]{1,100} is ${SIZE}\\.\\))?$`)

/** Plain words for a failed publish call: the hub's own refusals first, then the shares page's. */
export function publishError(e: unknown): string {
  if (e instanceof ApiError && HUB_TEXT[e.code]) return HUB_TEXT[e.code]
  if (e instanceof ApiError && e.code === 'quota.host' && HUB_TOO_BIG.test(e.message)) return `${e.message} Nothing was shared.`
  return shareError(e)
}
