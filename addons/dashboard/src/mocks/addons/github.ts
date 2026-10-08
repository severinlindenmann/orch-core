import { registerAddon } from './registry'

registerAddon({
  name: 'github',
  seed: () => ({ settings: { org: 'acme', link_prs: true } }),
  actions: {
    import: ({ store, ws, body }) => store.importGithubIssue(ws, (body.item ?? {}) as { title?: string; subtitle?: string; badge?: string }),
    save_settings: {
      minRole: 'owner',
      run: ({ state, body }) => {
        state.settings = body.formData ?? {}
        return { ok: true, message: 'Settings saved.', changed: true }
      },
    },
    refresh: () => ({ ok: true, message: 'Checked GitHub: 2 pull requests updated.' }),
  },
})
