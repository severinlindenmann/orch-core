import { registerAddon } from './registry'

registerAddon({
  name: 'estimate',
  seed: () => ({}),
  actions: {
    set({ store, ticket, body }) {
      const points = Number((body.formData as { points?: unknown } | undefined)?.points)
      if (!ticket || !Number.isFinite(points)) return { ok: true, message: 'Nothing to save.' }
      store.setAddonData(ticket, 'estimate', (d) => (d.points = points))
      store.append(ticket, { type: 'estimate.set', actor: { kind: 'addon', id: 'estimate' }, points })
      return { ok: true, message: `${ticket} estimated at ${points} points.`, changed: true }
    },
    save_settings: () => ({ ok: true, message: 'Settings saved (mock).' }),
  },
})
