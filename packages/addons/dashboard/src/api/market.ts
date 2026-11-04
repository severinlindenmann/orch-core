export type PublisherKey = 'unsigned' | 'new' | 'known' | 'changed'
export interface MarketAddon {
  id: string
  version: string
  capabilities: string[]
  revoked: boolean
  publisher: string | null
  from_marketplace: { summary: string }
  installed: { version: string; state: string } | null
  update: boolean
  blocked?: boolean
}
export interface Market {
  url: string
  class: 'official' | 'community'
  pinned: boolean
  index_commit: string
  last_fetch: number | null
  cache: 'ok' | 'invalid' | 'not_fetched'
  /** The official marketplace every workspace has: fetched on the first visit, never removable. */
  preset?: boolean
  version: number
  verified: boolean
  stale: boolean
  expires_at: string | null
  entries: number
  /** How many entries of this index the publisher withdrew (a count; each entry has its own flag). */
  revoked: number
  from_marketplace: { name: string; title: string }
  addons: MarketAddon[]
  allowed_by?: 'orch' | 'you' | 'hub'
  added_by?: string
  publisher_key?: PublisherKey
}
export interface InstalledAddon {
  name: string
  version: string
  package_sha256: string
  origin: string | null
  commit: string
  installed_at: number
  state: string
  publisher: string | null
  available: null | { version: string; package_sha256: string; capabilities_added: string[]; revoked: boolean; stale: boolean }
}
export type Changes = Record<'capabilities' | 'fields' | 'sections' | 'needs' | 'artifact_kinds', string[]>
export interface InstallSettingSchema {
  type: 'object' | 'string' | 'integer' | 'boolean' | 'array'
  title?: string
  default?: unknown
  properties?: Record<string, InstallSettingSchema>
  required?: string[]
  enum?: string[]
}
export interface MarketStage {
  grant: Record<string, unknown>
  settings_schema?: InstallSettingSchema | null
  stage: string
  expires_at: number
  name: string
  version: string
  package_sha256: string
  review: string[]
  market: {
    url: string
    class: 'official' | 'community' | 'dev'
    commit: string
    source_note: string
    publisher: { fingerprint: string | null; tofu: 'unsigned' | 'new' | 'known' }
    index_version: number
    stale: boolean
    summary: string | null
  }
  update: null | {
    from: { version: string; package_sha256: string; commit: string }
    changes: string[]
    added: Changes
    removed: Changes
    widened: boolean
    publisher: 'same' | 'first_use' | 'unsigned'
  }
  deps?: { count: number; verified: boolean }
}
export type StageInput = { id: string; url?: string; update?: boolean }
export type StageResult = MarketStage | { up_to_date: true; name: string; version: string }
/** `offered`: sources the host would let this workspace add and that are not added yet (the picker's list). */
export type MarketList = { markets: Market[]; offered?: { url: string; class: string }[]; open_address?: boolean }
export type MarketInstalled = { addons: InstalledAddon[] }
export const EXTERNAL_HELP = "External marketplaces aren't available in this workspace yet"
export function stateLabel(state?: string, pending = false): string {
  return pending || state === 'pending'
    ? 'Waiting for installation approval'
    : !state
      ? 'Available'
      : state === 'disabled'
        ? 'Off'
        : state === 'active' || state === 'enabled'
          ? 'On'
          : 'Installed'
}
export function permissionEffect(value: string): string {
  const effects: Record<string, string> = {
    network: 'Connects to the internet',
    serve_http: 'Serves its own web page',
    spawn_agent: 'Starts agents',
    pty: 'Opens terminals',
    git_push: 'Pushes code',
    secrets: 'Uses service tokens',
  }
  if (value.startsWith('network:')) return `Connects to ${value.slice(8)}`
  if (value.startsWith('secret:')) return `Uses your ${value.slice(7)} token`
  return effects[value] ?? `Requests permission: ${value}`
}
export function publisherWording(key: PublisherKey): string {
  return { unsigned: 'Unsigned', new: 'First use - key will be remembered', known: 'Publisher key matches the one previously saved on this computer', changed: 'Key changed - review required' }[key]
}
export function widened(stage: MarketStage): boolean {
  return !!stage.update && (stage.update.widened || stage.update.publisher === 'first_use' || Object.values(stage.update.added).some((a) => a.length > 0))
}
/** The host's review lines in the host's order. A line is marked NEW only from the structured diff (`update.added`):
 * a `capabilities <list> (...)` or `field <name> (...)` line whose listed names include an added value, compared whole. */
export function reviewLines(stage: MarketStage): { text: string; added: boolean }[] {
  const added = stage.update?.added
  const names = (text: string, kind: 'capabilities' | 'field'): string[] | null => {
    const m = kind === 'capabilities' ? /^capabilities ([^(]*?)(?: \(|$)/.exec(text) : /^field (.+?)(?: \(|$)/.exec(text)
    return m ? (kind === 'field' ? [m[1]] : m[1].split(/[\s,]+/).filter(Boolean)) : null
  }
  return stage.review.map((text) => {
    const caps = names(text, 'capabilities')
    const field = names(text, 'field')
    return {
      text,
      added: !!added && ((!!caps && caps.some((c) => added.capabilities.includes(c))) || (!!field && field.some((f) => added.fields.includes(f)))),
    }
  })
}
export function marketError(error: unknown): string {
  const code = (error as { code?: string })?.code
  if (code === 'market.community_unisolated' || code === 'market.not_allowed') return EXTERNAL_HELP
  if (code === 'market.still_enabled') return 'Turn it off in Settings > Add-ons first'
  if (code === 'market.publisher_key_changed') return 'The publisher key changed. Review is required.'
  return 'The marketplace request could not be completed. Try again.'
}

/** Core reports enabled; older dashboard fixtures use active. */
export const isEnabled = (state: string | undefined) => state === 'enabled' || state === 'active'
