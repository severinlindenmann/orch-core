import { registerAddon } from './registry'

registerAddon({
  name: 'usage',
  seed: () => ({ settings: { weekly_budget_chf: 50 } }),
  actions: {
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
