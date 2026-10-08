import { registerAddon } from './registry'

registerAddon({
  name: 'publish',
  seed: () => ({ apps: [], shares: [] }),
  actions: {
    share({ store, ticket, state }) {
      if (!ticket || !store.hasTicket(ticket)) return { ok: true, message: 'Pick a ticket first.' }
      let n = 0
      store.setAddonData(ticket, 'publish', (d) => {
        const shares = (d.shares as unknown[] | undefined) ?? []
        shares.unshift({ title: `share/${ticket.toLowerCase()}-${shares.length + 1}`, subtitle: 'expires in 7 days · 0 views', badge: 'secret link' })
        d.shares = shares
        n = shares.length
      })
      const shares = (state.shares as unknown[] | undefined) ?? []
      shares.unshift({ ticket, title: `share/${ticket.toLowerCase()}-${n}`, expires_in_days: 7, views: 0 })
      state.shares = shares
      store.append(ticket, { type: 'publish.shared', actor: { kind: 'addon', id: 'publish' } })
      return { ok: true, message: `Shared ${ticket} as a secret link for 7 days.`, changed: true }
    },
    decide({ store, body }) {
      const option = String(body.option ?? '')
      const decisions = store.addons.find((a) => a.name === 'publish')?.decisions
      if (decisions) {
        const i = decisions.findIndex((d) => d.id === body.id)
        if (i >= 0) {
          const [done] = decisions.splice(i, 1)
          if (done.ticket && store.hasTicket(done.ticket)) store.append(done.ticket, { type: 'publish.decided', actor: { kind: 'addon', id: 'publish' }, option })
        }
      }
      return { ok: true, message: option === 'yes' ? 'Published as a secret link for 7 days.' : 'Not published.', changed: true }
    },
    save_settings: () => ({ ok: true, message: 'Settings saved (mock).' }),
  },
})
