import type { MockStore } from '@/mocks/store'

/** The catalog ships no add-on update, so a test offers one: `name` gets a newer version that adds `spawn_agent`. */
export function offerUpdate(store: MockStore, name = 'estimate', version = '0.6.0') {
  const pkg = store.addons.find((a) => a.name === name)!
  pkg.update = {
    version,
    capabilities: [...pkg.capabilities, 'spawn_agent'],
    package_sha256: '5'.repeat(64),
    changelog: 'Can now start an agent session. Adds the spawn_agent capability.',
    actions: pkg.actions,
  }
  return pkg
}
