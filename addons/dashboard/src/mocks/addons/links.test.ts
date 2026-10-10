import { describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'

// Workspace links (Preview, owner feedback 2026-10-10 B): pairing, requests as core decisions, handoffs, log, revoke.
const setup = (viewer = 'p_sev', install = true, dataset: 'normal' | 'busy' = 'normal') => {
  const store = createMockStore({ persist: false, dataset })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  if (install && dataset === 'normal') installAndGrant(store, ws, 'links')
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
type Node = { type: string; [k: string]: unknown }
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'links', id, body)
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string }) => `${e.status} ${e.code}`)
const raw = (s: S) => s.store.addonState(s.ws, 'links') as Record<string, unknown> & { links: { id: string; peer: { name: string }; revoked?: unknown; accepts: string[] }[]; requests: { id: string; state: string; ticket?: string }[]; log: { type: string; link: string | null; ticket?: string }[]; pending?: { id: string; joined?: { fingerprint: string } } }
const page = async (s: S) => ((await s.api.getAddonState(s.ws, 'links')) as unknown as { page: Node }).page
/** Every node of a page, depth first. */
function nodes(n: unknown): Node[] {
  if (!n || typeof n !== 'object') return []
  const x = n as Node
  const kids = [...((x.children as unknown[]) ?? []), ...((x.tabs as { node: unknown }[]) ?? []).map((t) => t.node)]
  return [x, ...kids.flatMap(nodes)]
}
const rowsOf = (p: Node, firstKey: string) => nodes(p).filter((n) => n.type === 'table' && (n.columns as { key: string }[])[0].key === firstKey).flatMap((n) => n.rows as Record<string, unknown>[])
const decisions = (s: S) => s.api.getAddonDecisions(s.ws).then((d) => d.filter((x) => x.addon === 'links'))
const sign = (s: S, id: string, body: Record<string, unknown>) => run(s, id, { ...body, confirmed: true })

describe('links package', () => {
  it('starts in the catalog as a preview; minRoles, signatures and decisions live in the manifest', () => {
    const { store, ws } = setup('p_sev', false)
    expect(store.workspaces.find((w) => w.id === ws)!.addons.links).toBeUndefined()
    const pkg = store.addons.find((a) => a.name === 'links')!
    expect(pkg).toMatchObject({ title: 'Workspace links', preview: true, capabilities: ['network'] })
    expect(pkg.actions).toMatchObject({
      start_pairing: { minRole: 'owner' },
      confirm_pairing: { minRole: 'owner', confirm: 'sign' },
      revoke: { minRole: 'owner', confirm: 'destructive' },
      send_handoff: { minRole: 'member', confirm: 'sign' },
      decide: { decision: true },
      select_link: { minRole: 'viewer', kind: 'navigation' },
    })
  })
  it('is refused while inactive', async () => {
    const s = setup('p_sev', false)
    expect(await fail(s.api.getAddonState(s.ws, 'links'))).toBe('409 addon.inactive')
  })
  it('the busy day installs it in DEMO with more links, requests and a long log', () => {
    const s = setup('p_sev', true, 'busy')
    const st = raw(s)
    expect(st.links.length).toBeGreaterThanOrEqual(9)
    expect(st.links.some((l) => l.id === 'ln_cli')).toBe(true)
    expect(st.requests.filter((r) => r.state === 'open').length).toBeGreaterThanOrEqual(10)
    expect(st.log.length).toBeGreaterThan(100)
  })
})

describe('page', () => {
  it('has four tabs: Links, Requests, Log, Set up', async () => {
    const p = await page(setup())
    const tabs = nodes(p).find((n) => n.type === 'tabs')!.tabs as { id: string; label: string; count?: number }[]
    expect(tabs.map((t) => t.label)).toEqual(['Links', 'Requests', 'Log', 'Set up'])
    expect(tabs[0].count).toBe(2) // INT and Northwind; Contoso is revoked
  })
  it('shows each link with its carrier, scopes both ways, expiry and state', async () => {
    const rows = rowsOf(await page(setup()), 'peer')
    expect(rows.find((r) => r.id === 'ln_int')).toMatchObject({ carrier: 'Same machine', theySend: 'ticket handoffs, questions, status updates', state: 'active' })
    // The relay is off in the mock until an owner connects it: relay links say their messages wait.
    expect(rows.find((r) => r.id === 'ln_north')).toMatchObject({ carrier: 'Relay · off: messages wait', state: 'expiring', expires: 'in 5 days' })
    expect(rows.find((r) => r.id === 'ln_contoso')).toMatchObject({ state: 'revoked', canRevoke: false })
  })
  it('log rows tied to a restricted ticket are hidden from people who cannot see it', async () => {
    const owner = rowsOf(await page(setup('p_sev')), 'when')
    expect(owner.some((r) => r.ticket === 'DEMO-0044')).toBe(true)
    const tom = rowsOf(await page(setup('p_tom')), 'when')
    expect(tom.some((r) => r.ticket === 'DEMO-0044' || String(r.text).includes('DEMO-0044'))).toBe(false)
    // The raw state never leaves the host: view() replaces it.
    const st = (await setup('p_tom').api.getAddonState(setup('p_tom').ws, 'links')) as unknown as { log: unknown[]; requests: unknown[] }
    expect(st.log).toEqual([])
    expect(st.requests).toEqual([])
  })
  it('Set up is for owners; others read why', async () => {
    const setupOf = async (v: string) => nodes((nodes(await page(setup(v))).find((n) => n.type === 'tabs')!.tabs as { id: string; node: Node }[]).find((t) => t.id === 'setup')!.node)
    expect((await setupOf('p_sev')).some((n) => n.type === 'form' && n.action === 'start_pairing')).toBe(true)
    expect((await setupOf('p_mara')).some((n) => n.type === 'alert' && n.title === 'Owners set up links')).toBe(true)
  })
})

describe('decisions (core renders and signs them)', () => {
  it('owners get the pairing and scope requests; maintainers only handoffs and questions; viewers none', async () => {
    const ids = async (v: string) => (await decisions(setup(v))).map((d) => d.id).sort()
    const owner = await ids('p_sev')
    expect(owner).toHaveLength(4)
    expect(owner.find((id) => id.startsWith('pair.rq_fab.'))).toMatch(/^pair\.rq_fab\.[0-9A-Z]{6}$/)
    expect(await ids('p_mara')).toEqual(['req.rq_int_meter', 'req.rq_north_csv'])
    expect(await ids('p_tom')).toEqual([])
  })
  it('a maintainer cannot answer a pairing request (closed for them)', async () => {
    const s = setup()
    const id = (await decisions(s)).find((d) => d.id.startsWith('pair.'))!.id
    s.store.setViewer('p_mara')
    expect(await fail(sign(s, 'decide', { id, option: 'accept' }))).toBe('409 decision.closed')
  })
  it('accepting a pairing request makes an active link; core records addon.decided, the addon its links.paired', async () => {
    const s = setup()
    const id = (await decisions(s)).find((d) => d.id.startsWith('pair.'))!.id
    expect(await fail(run(s, 'decide', { id, option: 'accept' }))).toBe('409 confirm.required')
    expect((await sign(s, 'decide', { id, option: 'accept' })).message).toMatch(/Linked with Fabrikam/)
    expect(raw(s).links.find((l) => l.peer.name.startsWith('Fabrikam'))).toMatchObject({ accepts: ['question', 'drop'] })
    const evs = s.store.wsEventsOf(s.ws)
    expect(evs.find((e) => e.type === 'addon.decided' && e.name === 'links')).toMatchObject({ id, option: 'accept', presence: 'touchid' })
    expect(evs.find((e) => e.type === 'links.paired')).toMatchObject({ actor: { kind: 'addon', id: 'links' }, person: 'p_sev' })
    expect((await decisions(s)).some((d) => d.id === id)).toBe(false)
  })
  it('accepting a handoff makes a Backlog ticket marked from-peer', async () => {
    const s = setup('p_mara')
    const r = await sign(s, 'decide', { id: 'req.rq_int_meter', option: 'accept' })
    expect(r.ticket).toMatch(/^DEMO-/)
    const t = s.store.ticket(r.ticket!)!
    expect(t).toMatchObject({ status: 'backlog', labels: ['from-peer'], title: 'Meter schema v3: update the DEMO import' })
    expect(s.store.eventsOf(t.key).some((e) => e.type === 'links.received' && e.actor.kind === 'addon')).toBe(true)
  })
  it('a scope request widens what the peer may send; an answer is logged', async () => {
    const s = setup()
    await sign(s, 'decide', { id: 'req.rq_int_drop', option: 'allow' })
    expect(raw(s).links.find((l) => l.id === 'ln_int')!.accepts).toContain('drop')
    await sign(s, 'decide', { id: 'req.rq_north_csv', option: 'both' })
    expect(raw(s).log.at(-1)).toMatchObject({ type: 'links.answered', link: 'ln_north' })
    expect(await fail(sign(s, 'decide', { id: 'req.rq_north_csv', option: 'csv' }))).toBe('409 decision.closed')
  })
})

describe('pairing (Set up)', () => {
  const start = (s: S, formData: Record<string, unknown>) => run(s, 'start_pairing', { formData: { days: 90, handoff: true, question: true, status: true, drop: false, ...formData } })
  it('owner only; checks the carrier against where the peer runs; no direct path', async () => {
    const m = setup('p_mara')
    expect(await fail(start(m, { carrier: 'relay', peer: 'other', name: 'Fabrikam' }))).toBe('403 forbidden')
    const s = setup()
    expect(await fail(start(s, { carrier: 'local', peer: 'ws:CLI' }))).toBe('400 validation')
    expect(await fail(start(s, { carrier: 'local', peer: 'other', name: 'Fabrikam' }))).toBe('400 validation')
    expect(await fail(start(s, { carrier: 'direct', peer: 'ws:CLI' }))).toBe('400 validation')
    expect(await fail(start(s, { carrier: 'local', peer: 'ws:INT' }))).toBe('409 links.exists')
  })
  it('through the relay: code → the other owner joins once the relay is online → comparison code → signed link', async () => {
    vi.useFakeTimers()
    try {
      const s = setup()
      expect((await start(s, { carrier: 'relay', peer: 'ws:CLI' })).message).toMatch(/Pairing code ready/)
      expect(await fail(start(s, { carrier: 'relay', peer: 'other', name: 'X Y' }))).toBe('409 links.pairing_open')
      expect(await fail(run(s, 'simulate_peer'))).toBe('409 links.relay_offline')
      s.store.appendWs(s.ws, { type: 'relay.connected' })
      vi.advanceTimersByTime(3_000)
      await run(s, 'simulate_peer')
      const p = raw(s).pending!
      expect(p.joined!.fingerprint).toMatch(/^[0-9A-Z]{6}$/)
      await sign(s, 'confirm_pairing', { pairing: p.id, code: p.joined!.fingerprint })
      expect(rowsOf(await page(s), 'peer').find((r) => r.peer === 'CLI · Client VM')).toMatchObject({ carrier: 'Relay · online', state: 'active' })
      // The code is single use and short-lived: a new pairing after 10 minutes cannot be joined.
      await start(s, { carrier: 'relay', peer: 'other', name: 'Late Co', owner: 'Kim' })
      vi.advanceTimersByTime(11 * 60_000)
      expect(await fail(run(s, 'simulate_peer'))).toBe('409 links.code_expired')
    } finally {
      vi.useRealTimers()
    }
  })
  it('a same-machine pairing needs no relay and ends in a signed link', async () => {
    const s = setup()
    // INT is linked already; revoke it first, then pair again on this machine.
    await run(s, 'revoke', { id: 'ln_int', confirmed: true })
    await start(s, { carrier: 'local', peer: 'ws:INT' })
    await run(s, 'simulate_peer')
    const p = raw(s).pending!
    expect(await fail(run(s, 'confirm_pairing', { pairing: p.id, code: p.joined!.fingerprint }))).toBe('409 confirm.required')
    expect(await fail(sign(s, 'confirm_pairing', { pairing: p.id, code: 'AAAAAA' }))).toBe('409 links.code_mismatch')
    expect((await sign(s, 'confirm_pairing', { pairing: p.id, code: p.joined!.fingerprint })).message).toBe('Linked with INT · Internal.')
    expect(raw(s).pending).toBeUndefined()
    expect(s.store.wsEventsOf(s.ws).find((e) => e.type === 'addon.action_signed' && e.action === 'confirm_pairing')).toMatchObject({ args: { pairing: p.id, code: p.joined!.fingerprint } })
  })
})

describe('handoffs and revoke', () => {
  it('restricted tickets never cross (409 links.restricted), on review and on send', async () => {
    const s = setup()
    expect(await fail(run(s, 'prepare_handoff', { formData: { link: 'ln_int', ticket: 'DEMO-0044' } }))).toBe('409 links.restricted')
    expect(await fail(sign(s, 'send_handoff', { link: 'ln_int', ticket: 'DEMO-0044' }))).toBe('409 links.restricted')
  })
  it('a member signs a handoff; a peer that does not accept handoffs is refused; viewers cannot', async () => {
    const s = setup('p_mara')
    expect(await fail(sign(s, 'send_handoff', { link: 'ln_north', ticket: 'DEMO-0045' }))).toBe('409 links.scope')
    expect(await fail(run(s, 'send_handoff', { link: 'ln_int', ticket: 'DEMO-0045' }))).toBe('409 confirm.required')
    expect((await sign(s, 'send_handoff', { link: 'ln_int', ticket: 'DEMO-0045' })).message).toMatch(/Handed off DEMO-0045 to INT/)
    expect(await fail(sign(s, 'send_handoff', { link: 'ln_int', ticket: 'DEMO-0045' }))).toBe('409 links.already_sent')
    expect(s.store.eventsOf('DEMO-0045').at(-1)).toMatchObject({ type: 'links.sent', actor: { kind: 'addon', id: 'links' } })
    const tom = setup('p_tom')
    expect(await fail(sign(tom, 'send_handoff', { link: 'ln_int', ticket: 'DEMO-0045' }))).toBe('403 forbidden')
  })
  it('revoke is an owner\'s, confirmed by core; it closes open requests and stops the link', async () => {
    const m = setup('p_mara')
    expect(await fail(run(m, 'revoke', { id: 'ln_int', confirmed: true }))).toBe('403 forbidden')
    const s = setup()
    expect(await fail(run(s, 'revoke', { id: 'ln_int' }))).toBe('409 confirm.required')
    await run(s, 'revoke', { id: 'ln_int', confirmed: true })
    expect(raw(s).requests.filter((r) => r.id.startsWith('rq_int')).map((r) => r.state)).toEqual(['closed', 'closed'])
    expect(await fail(sign(s, 'send_handoff', { link: 'ln_int', ticket: 'DEMO-0045' }))).toBe('409 links.revoked')
    expect((await decisions(s)).some((d) => d.id.includes('rq_int'))).toBe(false)
    expect(s.store.wsEventsOf(s.ws).at(-1)).toMatchObject({ type: 'links.revoked', actor: { kind: 'addon', id: 'links' } })
  })
})
