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
  last_fetch: string | null
  cache: 'ok' | 'invalid'
  version: number
  verified: boolean
  stale: boolean
  expires_at: string | null
  entries: number
  revoked: boolean
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
  installed_at: string
  state: string
  publisher: string | null
  available: null | { version: string; package_sha256: string; capabilities_added: string[]; revoked: boolean; stale: boolean }
}
export type Changes = Record<'capabilities' | 'fields' | 'sections' | 'needs' | 'artifact_kinds', string[]>
export interface MarketStage {
  stage: string
  expires_at: string
  name: string
  version: string
  package_sha256: string
  review: string[]
  market: {
    url: string
    class: 'official' | 'community'
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
export type MarketList = { markets: Market[] }
export type MarketInstalled = { addons: InstalledAddon[] }
export const EXTERNAL_HELP = "External marketplaces aren't available in this workspace yet"
export function stateLabel(state?: string, pending = false): string {
  return pending || state === 'pending'
    ? 'Awaiting Terminal approval'
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
    internet: 'Connects to the internet',
    fields: 'Adds fields to tickets',
    sections: 'Adds sections to tickets',
    spawn: 'Starts agents',
    secrets: 'Uses service tokens',
    'ticket.fields': 'Adds fields to tickets',
  }
  if (value.startsWith('network:')) return `Connects to ${value.slice(8)}`
  if (value.startsWith('secret:')) return `Uses your ${value.slice(7)} token`
  return effects[value] ?? `Requests permission: ${value}`
}
export function publisherWording(key: PublisherKey): string {
  return { unsigned: 'Unsigned', new: 'First use - key will be remembered', known: 'Matches remembered key', changed: 'Key changed - review required' }[key]
}
export function widened(stage: MarketStage): boolean {
  return !!stage.update && (stage.update.widened || stage.update.publisher === 'first_use' || Object.values(stage.update.added).some((a) => a.length > 0))
}
export function reviewLines(stage: MarketStage): { text: string; added: boolean }[] {
  const added = Object.values(stage.update?.added ?? {}).flat() as string[]
  return stage.review
    .map((text) => ({ text, added: added.some((value) => text.includes(value) || text === permissionEffect(value)) }))
    .sort((a, b) => Number(b.added) - Number(a.added))
}
export function marketError(error: unknown): string {
  const code = (error as { code?: string })?.code
  if (code === 'market.community_unisolated' || code === 'market.not_allowed') return EXTERNAL_HELP
  if (code === 'market.still_enabled') return 'Turn it off in Settings > Add-ons first'
  if (code === 'market.publisher_key_changed') return 'The publisher key changed. Review is required.'
  return 'The marketplace request could not be completed. Try again.'
}
export const validMarketUrl = (url: string) => /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/?$/.test(url)
