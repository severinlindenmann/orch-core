import { describe, expect, it } from 'vitest'
import { createMockStore } from './store'

// Today shares derived documents within one request. The snapshot must never outlive that request or its viewer.
describe('Today request snapshot', () => {
  it.each(['normal', 'busy'] as const)('keeps attention rules and visibility fresh in %s data', dataset => {
    const store = createMockStore({ persist: false, dataset })
    const ws = store.workspaces.find(w => w.prefix === 'DEMO')!.id
    for (const viewer of ['p_sev', 'p_mara', 'p_tom', 'p_sev']) {
      store.setViewer(viewer)
      const today = store.today(ws)
      expect(today.needs_you).toEqual(store.needsYou(ws))
      expect(today.read_only_open).toEqual(store.readOnlyOpen(ws))
      expect(today.waiting_on_others).toEqual(store.waitingOnOthers(ws))
      for (const row of [...today.needs_you, ...today.read_only_open, ...today.recent]) expect(store.isVisible(row.ticket, viewer)).toBe(true)
    }
    const before = store.today(ws)
    const key = before.needs_you[0].ticket
    store.append(key, { type: 'status.changed', to: 'done', actor: 'p_sev' })
    const after = store.today(ws)
    expect(after.needs_you.some(row => row.ticket === key)).toBe(false)
    expect(after.counts.done).toBe((before.counts.done ?? 0) + 1)
  })
})
