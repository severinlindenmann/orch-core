import { registerAddon } from './registry'

registerAddon({
  name: 'usage',
  seed: () => ({}),
  actions: { save_settings: () => ({ ok: true, message: 'Settings saved (mock).' }) },
})
