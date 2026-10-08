import { registerAddon } from './registry'

registerAddon({
  name: 'terminals',
  seed: () => ({ settings: { shell: '/bin/zsh' } }),
  actions: {
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
    open: () => ({ ok: true, message: 'Terminals arrive in a later iteration.' }),
  },
})
