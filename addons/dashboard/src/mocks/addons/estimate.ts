// Estimate: points on tickets, in the workspace's scale.
//  - Scales: fibonacci (1 2 3 5 8 13 21, the default), linear (1..10), t-shirt (XS S M L XL).
//  - Points stay in the ticket's addon data (`ticket.addons.estimate = { points, weight }`): the Board's card field
//    binds against the ticket summary only, so it cannot read addon state. `weight` is the number the Board sums:
//    the points themselves, or for t-shirt sizes the fixed mapping XS=1 S=2 M=3 L=5 XL=8.
//  - The ticket panel's choices come from the scale (view() exposes `pointsSchema`).
import { registerAddon } from './registry'

const SCALES: Record<string, (string | number)[]> = {
  fibonacci: [1, 2, 3, 5, 8, 13, 21],
  linear: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
  't-shirt': ['XS', 'S', 'M', 'L', 'XL'],
}
const TSHIRT_WEIGHT: Record<string, number> = { XS: 1, S: 2, M: 3, L: 5, XL: 8 }

const scaleOf = (state: Record<string, unknown>) => {
  const s = (state.settings as { scale?: string } | undefined)?.scale
  return s && Object.hasOwn(SCALES, s) ? s : 'fibonacci'
}
const weightOf = (v: string | number) => (typeof v === 'number' ? v : TSHIRT_WEIGHT[v])

registerAddon({
  name: 'estimate',
  seed: () => ({ settings: { scale: 'fibonacci' } }),
  view(state) {
    const scale = scaleOf(state)
    const options = SCALES[scale]
    return {
      scale,
      pointsSchema: {
        type: 'object',
        properties: { points: { type: typeof options[0] === 'number' ? 'integer' : 'string', title: 'Points', enum: options } },
      },
    }
  },
  actions: {
    set({ store, ticket, body, state }) {
      const raw = (body.formData as { points?: unknown } | undefined)?.points
      if (!ticket || raw === undefined || raw === null || raw === '') return { ok: true, message: 'Nothing to save.' }
      const scale = scaleOf(state)
      const points = SCALES[scale].find((o) => String(o) === String(raw))
      if (points === undefined) return { ok: true, message: `${String(raw)} is not on the ${scale} scale.` }
      store.setAddonData(ticket, 'estimate', (d) => {
        d.points = points
        d.weight = weightOf(points)
      })
      store.append(ticket, { type: 'estimate.set', actor: { kind: 'addon', id: 'estimate' }, points })
      return { ok: true, message: `${ticket} estimated at ${points}${typeof points === 'number' ? ' points' : ''}.`, changed: true }
    },
    save_settings: ({ state, body }) => {
      state.settings = body.formData ?? {}
      return { ok: true, message: 'Settings saved.', changed: true }
    },
  },
})
