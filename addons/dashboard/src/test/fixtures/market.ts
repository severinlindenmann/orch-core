import type { Changes, InstalledAddon, Market, MarketStage } from '@/api/market'
export const emptyChanges = (): Changes => ({ capabilities: [], fields: [], sections: [], needs: [], artifact_kinds: [] })
export const market = (over: Partial<Market> = {}): Market => ({
  url: 'https://github.com/demo-example/market',
  class: 'official',
  pinned: true,
  index_commit: 'demo-commit',
  last_fetch: 1_790_000_000,
  cache: 'ok',
  version: 1,
  verified: true,
  stale: false,
  expires_at: null,
  entries: 1,
  revoked: 0,
  from_marketplace: { name: 'orch-marketplace', title: 'Official marketplace' },
  addons: [
    {
      id: 'calendar',
      version: '1.2.0',
      capabilities: ['network'],
      revoked: false,
      publisher: null,
      from_marketplace: { summary: 'Connect calendars to tickets.' },
      installed: null,
      update: false,
    },
  ],
  ...over,
})
export const stage = (over: Partial<MarketStage> = {}): MarketStage => ({
  stage: 'demo-stage',
  expires_at: '2099-01-01T00:00:00Z',
  name: 'calendar',
  version: '1.2.0',
  package_sha256: 'demo-package-digest',
  review: ['Adds fields to tickets', 'Connects to calendar.example.test'],
  market: {
    url: market().url,
    class: 'official',
    commit: 'demo-commit',
    source_note: 'History not checked',
    publisher: { fingerprint: null, tofu: 'unsigned' },
    index_version: 1,
    stale: false,
    summary: 'Calendar integration',
  },
  update: null,
  deps: { count: 2, verified: true },
  ...over,
})
export const installedAddon = (over: Partial<InstalledAddon> = {}): InstalledAddon => ({
  name: 'calendar',
  version: '1.0.0',
  package_sha256: 'demo-old-digest',
  origin: market().url,
  commit: 'demo-old-commit',
  installed_at: 1_790_000_000,
  state: 'disabled',
  publisher: null,
  available: { version: '1.2.0', package_sha256: 'demo-package-digest', capabilities_added: ['network'], revoked: false, stale: false },
  ...over,
})
export const updateStage = (wide: boolean) =>
  stage({
    update: {
      from: { version: '1.0.0', package_sha256: 'demo-old-digest', commit: 'demo-old-commit' },
      changes: ['Dependency lock updated'],
      added: { ...emptyChanges(), ...(wide ? { capabilities: ['network:calendar.example.test'], fields: ['Calendar link'] } : {}) },
      removed: { ...emptyChanges(), fields: ['Old link'] },
      widened: wide,
      publisher: 'same',
    },
  })
