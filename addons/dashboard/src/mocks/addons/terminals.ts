import { registerAddon } from './registry'

registerAddon({
  name: 'terminals',
  seed: () => ({}),
  actions: {
    save_settings: () => ({ ok: true, message: 'Settings saved (mock).' }),
    open: () => ({ ok: true, message: 'Terminals arrive in a later iteration.' }),
  },
})
