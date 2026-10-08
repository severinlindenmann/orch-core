import { registerAddon } from './registry'

registerAddon({
  name: 'wiki',
  seed: () => ({ settings: {} }),
  actions: {
    open: () => ({ ok: true, message: 'Wiki editor arrives in a later iteration.' }),
    save_settings: {
      minRole: 'owner',
      run: ({ state, body }) => {
        state.settings = body.formData ?? {}
        return { ok: true, message: 'Settings saved.', changed: true }
      },
    },
  },
})
