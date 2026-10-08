import { registerAddon } from './registry'

registerAddon({
  name: 'github',
  seed: () => ({}),
  actions: {
    import: ({ store, body }) => store.importGithubIssue((body.item ?? {}) as { title?: string; subtitle?: string; badge?: string }),
    refresh: () => ({ ok: true, message: 'Checked GitHub: 2 pull requests updated.' }),
  },
})
