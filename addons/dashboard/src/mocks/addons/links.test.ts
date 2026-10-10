import { afterEach, describe, expect, it, vi } from 'vitest'
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
type Raw = Record<string, unknown> & {
  links: { id: string; peer: { name: string }; revoked?: unknown; accepts: string[]; theyAccept: string[]; expires_at: string; paired_at: string }[]
  requests: { id: string; state: string; ticket?: string }[]
  sent: { id: string; link: string; state: string }[]
  log: { type: string; link: string | null; ticket?: string }[]
  pending?: { id: string; joined?: { fingerprint: string } }
}
const raw = (s: S) => s.store.addonState(s.ws, 'links') as Raw
const page = async (s: S) => ((await s.api.getAddonState(s.ws, 'links')) as unknown as { page: Node }).page
/** Every node of a page, depth first. */
function nodes(n: unknown): Node[] {
  if (!n || typeof n !== 'object') return []
  const x = n as Node
  const kids = [...((x.children as unknown[]) ?? []), ...((x.tabs as { node: unknown }[]) ?? []).map((t) => t.node)]
  return [x, ...kids.flatMap(nodes)]
}
const rowsOf = (p: Node, firstKey: string) => nodes(p).filter((n) => n.type === 'table' && (n.columns as { key: string }[])[0].key === firstKey).flatMap((n) => n.rows as Record<string, unknown>[])
/** The args the page's button for `action` carries (what core shows in the signing covers). */
const buttonArgs = async (s: S, action: string) => (nodes(await page(s)).find((n) => n.type === 'button' && n.action === action)!.args ?? {}) as Record<string, string | number>
const decisions = (s: S) => s.api.getAddonDecisions(s.ws).then((d) => d.filter((x) => x.addon === 'links'))
const sign = (s: S, id: string, body: Record<string, unknown>) => run(s, id, { ...body, confirmed: true })
const pairingDecision = async (s: S) => (await decisions(s)).find((d) => d.id.startsWith('pair.'))!
/** What core posts after its prompt: the decision's id and terms, the option, `confirmed`. */
const answer = (s: S, d: { action: string; id: string; terms?: Record<string, string | number> }, option: string) => sign(s, d.action, { id: d.id, option, ...(d.terms ? { terms: d.terms } : {}) })
/** The relay online: connected, and 3 s of the mock clock for "connecting" to pass. Needs fake timers. */
const relayOnline = (s: S) => {
  s.store.appendWs(s.ws, { type: 'relay.connected' })
  vi.advanceTimersByTime(3_000)
}
afterEach(() => vi.useRealTimers())

describe('links package', () => {
  it('starts in the catalog as a preview; minRoles, signatures and decisions live in the manifest', () => {
    const { store, ws } = setup('p_sev', false)
    expect(store.workspaces.find((w) => w.id === ws)!.addons.links).toBeUndefined()
    const pkg = store.addons.find((a) => a.name === 'links')!
    expect(pkg).toMatchObject({ title: 'Workspace links', preview: true, capabilities: ['network'] })
    expect(pkg.actions).toMatchObject({
      start_pairing: { minRole: 'owner' },
      confirm_pairing: { minRole: 'owner', confirm: 'sign' },
      revoke: { minRole: 'owner', confirm: 'sign' },
      send_handoff: { minRole: 'member', confirm: 'sign' },
      decide: { decision: true },
      decide_pairing: { minRole: 'owner', decision: true },
      decide_scope: { minRole: 'owner', decision: true },
      set_pairing_terms: { minRole: 'owner' },
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
  it('the busy day: no request or handoff on a link that was not usable then; every links decision can be answered', async () => {
    vi.useFakeTimers()
    const s = setup('p_sev', true, 'busy')
    const st = raw(s)
    const live = new Set(st.links.filter((l) => !l.revoked && Date.parse(l.expires_at) > Date.parse('2026-10-09T11:30:00Z')).map((l) => l.id))
    for (const r of st.requests as { link?: string; state: string }[]) if (r.state === 'open' && r.link) expect(live.has(r.link), r.link).toBe(true)
    for (const x of st.sent.filter((y) => y.id.startsWith('out_b'))) expect(live.has(x.link), x.link).toBe(true)
    relayOnline(s)
    for (const d of await decisions(s)) expect(await fail(answer(s, d, d.options[0].key)), d.id).toBe('ok')
    expect(await decisions(s)).toEqual([])
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
    const t = setup('p_tom')
    const st = (await t.api.getAddonState(t.ws, 'links')) as unknown as { log: unknown[]; requests: unknown[] }
    expect(st.log).toEqual([])
    expect(st.requests).toEqual([])
  })
  it('Set up is for owners; others read why', async () => {
    const setupOf = async (v: string) => nodes((nodes(await page(setup(v))).find((n) => n.type === 'tabs')!.tabs as { id: string; node: Node }[]).find((t) => t.id === 'setup')!.node)
    expect((await setupOf('p_sev')).some((n) => n.type === 'form' && n.action === 'start_pairing')).toBe(true)
    expect((await setupOf('p_mara')).some((n) => n.type === 'alert' && n.title === 'Owners set up links')).toBe(true)
  })
  it('the handoff form never offers a restricted ticket', async () => {
    const form = nodes(await page(setup())).find((n) => n.type === 'form' && n.action === 'prepare_handoff')!
    const keys = ((form.schema as { properties: { ticket: { enum: string[] } } }).properties.ticket.enum)
    expect(keys).toContain('DEMO-0045')
    expect(keys).not.toContain('DEMO-0044')
  })
  it('a member decides nothing: no decisions and no "for you to decide"', async () => {
    const s = setup('p_tom')
    s.store.appendWs(s.ws, { type: 'member.role_changed', person: 'p_tom', role: 'member', from: 'viewer', actor: 'p_sev' })
    expect(await decisions(s)).toEqual([])
    const stat = nodes(await page(s)).find((n) => n.type === 'stat' && n.label === 'Open requests')!
    expect(stat.hint).toBe('owners and maintainers decide them')
  })
})

describe('decisions (core renders and signs them)', () => {
  it('owners get pairing and scope requests on their own actions; maintainers only handoffs and questions; viewers none', async () => {
    const s = setup('p_sev')
    const own = await decisions(s)
    expect(own.map((d) => [d.id, d.action]).sort()).toEqual([
      [expect.stringMatching(/^pair\.rq_fab\.[0-9A-Z]{6}\.relay\.recv-question\+drop\.send-handoff\+question\.90d$/), 'decide_pairing'],
      ['req.rq_int_drop', 'decide_scope'],
      ['req.rq_int_meter', 'decide'],
      ['req.rq_north_csv', 'decide'],
    ].sort())
    expect((await decisions(setup('p_mara'))).map((d) => d.id).sort()).toEqual(['req.rq_int_meter', 'req.rq_north_csv'])
    expect(await decisions(setup('p_tom'))).toEqual([])
  })
  it('a maintainer cannot answer a pairing request (closed for them, and below its minRole)', async () => {
    const s = setup()
    const id = (await pairingDecision(s)).id
    s.store.setViewer('p_mara')
    expect(await fail(sign(s, 'decide_pairing', { id, option: 'accept' }))).toBe('403 forbidden')
    expect(await fail(sign(s, 'decide', { id, option: 'accept' }))).toBe('409 decision.closed')
  })
  it('accepting a pairing needs the relay online; it makes a link on exactly the signed terms', async () => {
    vi.useFakeTimers()
    const s = setup()
    const d = await pairingDecision(s)
    expect(await fail(run(s, 'decide_pairing', { id: d.id, option: 'accept' }))).toBe('409 confirm.required')
    expect(await fail(answer(s, d, 'accept'))).toBe('409 links.relay_offline')
    relayOnline(s)
    // Core shows the terms line by line and posts them; an answer without them, or with others, is refused.
    expect(d.terms).toEqual({ peer: 'Fabrikam Energy · DATA', comparison_code: expect.stringMatching(/^[0-9A-Z]{6}$/), carrier: 'relay https://relay.dev.severin.io', they_may_send_us: 'questions, Drop files', we_may_send_them: 'ticket handoffs, questions', expires_after: '90 days' })
    expect(await fail(sign(s, 'decide_pairing', { id: d.id, option: 'accept' }))).toBe('409 decision.closed')
    expect(await fail(sign(s, 'decide_pairing', { id: d.id, option: 'accept', terms: { ...d.terms, expires_after: '365 days' } }))).toBe('409 decision.closed')
    expect((await answer(s, d, 'accept')).message).toMatch(/Linked with Fabrikam/)
    const l = raw(s).links.find((x) => x.peer.name.startsWith('Fabrikam'))!
    expect(l).toMatchObject({ accepts: ['question', 'drop'], theyAccept: ['handoff', 'question'] })
    expect(Date.parse(l.expires_at) - Date.parse(l.paired_at)).toBe(90 * 86_400_000)
    const evs = s.store.wsEventsOf(s.ws)
    expect(evs.find((e) => e.type === 'addon.decided' && e.name === 'links')).toMatchObject({ id: d.id, option: 'accept', terms: d.terms, presence: 'touchid' })
    expect(evs.find((e) => e.type === 'links.paired')).toMatchObject({ actor: { kind: 'addon', id: 'links' }, person: 'p_sev' })
    expect((await decisions(s)).some((x) => x.id === d.id)).toBe(false)
  })
  it('the owner narrows the terms and picks the expiry, never widens; the decision id follows the terms', async () => {
    vi.useFakeTimers()
    const s = setup()
    const before = await pairingDecision(s)
    expect(await fail(run(s, 'set_pairing_terms', { formData: { request: 'rq_fab', recv_handoff: true, days: 30 } }))).toBe('400 validation')
    await run(s, 'set_pairing_terms', { formData: { request: 'rq_fab', recv_question: true, recv_drop: false, send_question: true, days: 30 } })
    expect(raw(s).log.at(-1)).toMatchObject({ type: 'links.terms_set', by: 'p_sev' })
    const after = await pairingDecision(s)
    expect(after.id).toMatch(/\.relay\.recv-question\.send-question\.30d$/)
    expect(after.terms).toMatchObject({ they_may_send_us: 'questions', we_may_send_them: 'questions', expires_after: '30 days' })
    // A prompt opened on the old terms cannot be signed any more, and core says the terms changed.
    relayOnline(s)
    const stale = await answer(s, before, 'accept').then(() => null, (e: { code: string; message: string; hint?: string }) => e)
    expect(stale).toMatchObject({ code: 'decision.closed', message: 'That decision is closed, or its terms changed.', hint: 'Reopen it and check the terms again.' })
    await answer(s, after, 'accept')
    const l = raw(s).links.find((x) => x.peer.name.startsWith('Fabrikam'))!
    expect(l).toMatchObject({ accepts: ['question'], theyAccept: ['question'] })
    expect(Date.parse(l.expires_at) - Date.parse(l.paired_at)).toBe(30 * 86_400_000)
  })
  it('the default terms keep only the kinds this host knows (unknown kinds from the peer are dropped)', async () => {
    const s = setup()
    const r = (raw(s).requests as unknown as { id: string; wants: string[] }[]).find((x) => x.id === 'rq_fab')!
    r.wants = ['question', 'question.send-handoff', 'drop']
    const d = await pairingDecision(s)
    expect(d.id).toMatch(/\.recv-question\+drop\.send-handoff\+question\.90d$/)
    expect(d.terms!.they_may_send_us).toBe('questions, Drop files')
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
    await sign(s, 'decide_scope', { id: 'req.rq_int_drop', option: 'allow' })
    expect(raw(s).links.find((l) => l.id === 'ln_int')!.accepts).toContain('drop')
    await sign(s, 'decide', { id: 'req.rq_north_csv', option: 'both' })
    expect(raw(s).log.at(-1)).toMatchObject({ type: 'links.answered', link: 'ln_north' })
    expect(await fail(sign(s, 'decide', { id: 'req.rq_north_csv', option: 'csv' }))).toBe('409 decision.closed')
  })
})

describe('pairing (Set up)', () => {
  const start = (s: S, formData: Record<string, unknown>) => run(s, 'start_pairing', { formData: { days: 90, handoff: true, question: true, status: true, drop: false, ...formData } })
  it('owner only; checks the carrier against where the peer runs; no direct path; no hidden characters in names', async () => {
    const m = setup('p_mara')
    expect(await fail(start(m, { carrier: 'relay', peer: 'other', name: 'Fabrikam' }))).toBe('403 forbidden')
    const s = setup()
    expect(await fail(start(s, { carrier: 'local', peer: 'ws:CLI' }))).toBe('400 validation')
    expect(await fail(start(s, { carrier: 'local', peer: 'other', name: 'Fabrikam' }))).toBe('400 validation')
    expect(await fail(start(s, { carrier: 'direct', peer: 'ws:CLI' }))).toBe('400 validation')
    expect(await fail(start(s, { carrier: 'local', peer: 'ws:INT' }))).toBe('409 links.exists')
    expect(await fail(start(s, { carrier: 'relay', peer: 'other', name: 'Evil‮gnp.exe' }))).toBe('400 validation')
    expect(await fail(start(s, { carrier: 'relay', peer: 'other', name: 'Northwind Grid · OPS' }))).toBe('409 links.exists')
  })
  it('a peer name with markdown never becomes markdown on the page', async () => {
    const s = setup()
    await start(s, { carrier: 'relay', peer: 'other', name: '[x](https://evil.example)', owner: 'Kim' })
    const texts = nodes(await page(s)).filter((n) => n.type === 'markdown').map((n) => String(n.text))
    expect(texts.some((t) => t.includes('\\[x\\]\\(https://evil\\.example\\)'))).toBe(true)
    expect(texts.some((t) => t.includes('[x](https://evil.example)'))).toBe(false)
  })
  it('through the relay: code → the other owner joins once the relay is online → comparison code → signed terms', async () => {
    vi.useFakeTimers()
    const s = setup()
    expect((await start(s, { carrier: 'relay', peer: 'ws:CLI' })).message).toMatch(/Pairing code ready/)
    expect(await fail(start(s, { carrier: 'relay', peer: 'other', name: 'X Y' }))).toBe('409 links.pairing_open')
    expect(await fail(run(s, 'simulate_peer'))).toBe('409 links.relay_offline')
    relayOnline(s)
    await run(s, 'simulate_peer')
    const args = await buttonArgs(s, 'confirm_pairing')
    expect(args).toEqual({ pairing: raw(s).pending!.id, code: expect.stringMatching(/^[0-9A-Z]{6}$/), peer: 'CLI · Client VM', carrier: 'relay https://relay.dev.severin.io', they_may_send: 'ticket handoffs, questions, status updates', we_may_send: 'ticket handoffs, questions, status updates', expires_days: 90 })
    // Every shown value is checked: a different one is refused.
    expect(await fail(sign(s, 'confirm_pairing', { ...args, they_may_send: 'questions' }))).toBe('409 links.stale')
    expect(await fail(sign(s, 'confirm_pairing', { ...args, expires_days: 365 }))).toBe('409 links.stale')
    await sign(s, 'confirm_pairing', args)
    expect(rowsOf(await page(s), 'peer').find((r) => r.peer === 'CLI · Client VM')).toMatchObject({ carrier: 'Relay · online', state: 'active' })
    // The code is single use and short-lived: a new pairing after 10 minutes cannot be joined.
    await start(s, { carrier: 'relay', peer: 'other', name: 'Late Co', owner: 'Kim' })
    vi.advanceTimersByTime(11 * 60_000)
    expect(await fail(run(s, 'simulate_peer'))).toBe('409 links.code_expired')
  })
  it('a same-machine pairing needs no relay; the confirm window closes 10 minutes after the other side joined', async () => {
    vi.useFakeTimers()
    const s = setup()
    await sign(s, 'revoke', { id: 'ln_int', peer: 'INT · Internal' })
    await start(s, { carrier: 'local', peer: 'ws:INT' })
    await run(s, 'simulate_peer')
    const args = await buttonArgs(s, 'confirm_pairing')
    expect(args.code).toBe(raw(s).pending!.joined!.fingerprint)
    expect(await fail(run(s, 'confirm_pairing', args))).toBe('409 confirm.required')
    expect(await fail(sign(s, 'confirm_pairing', { ...args, code: 'AAAAAA' }))).toBe('409 links.code_mismatch')
    expect((await sign(s, 'confirm_pairing', args)).message).toBe('Linked with INT · Internal.')
    expect(raw(s).pending).toBeUndefined()
    expect(s.store.wsEventsOf(s.ws).find((e) => e.type === 'addon.action_signed' && e.action === 'confirm_pairing')).toMatchObject({ args })
    // Late signature: joined more than 10 minutes ago.
    await sign(s, 'revoke', { id: raw(s).links.at(-1)!.id, peer: 'INT · Internal' })
    await start(s, { carrier: 'local', peer: 'ws:INT' })
    await run(s, 'simulate_peer')
    const late = await buttonArgs(s, 'confirm_pairing')
    vi.advanceTimersByTime(11 * 60_000)
    expect(await fail(sign(s, 'confirm_pairing', late))).toBe('409 links.join_expired')
  })
})

describe('handoffs and revoke', () => {
  it('restricted tickets never cross (409 links.restricted), on review and on send', async () => {
    const s = setup()
    expect(await fail(run(s, 'prepare_handoff', { formData: { link: 'ln_int', ticket: 'DEMO-0044' } }))).toBe('409 links.restricted')
    expect(await fail(sign(s, 'send_handoff', { link: 'ln_int', ticket_key: 'DEMO-0044' }))).toBe('409 links.restricted')
  })
  it('a member signs a handoff whose covers list what crosses; a changed value, a peer without the scope and viewers are refused', async () => {
    const s = setup('p_mara')
    await run(s, 'prepare_handoff', { formData: { link: 'ln_int', ticket: 'DEMO-0045' } })
    const args = await buttonArgs(s, 'send_handoff')
    expect(args).toMatchObject({ link: 'ln_int', to: 'INT · Internal', carrier: 'spool (same machine)', ticket_key: 'DEMO-0045', title: 'Add outage windows to usage forecast', deadline: '3 days after sending' })
    expect(String(args.sends)).toMatch(/^title; sections: .+; attachments: /)
    expect(await fail(sign(s, 'send_handoff', { ...args, link: 'ln_north', to: 'Northwind Grid · OPS' }))).toBe('409 links.scope')
    expect(await fail(run(s, 'send_handoff', args))).toBe('409 confirm.required')
    expect(await fail(sign(s, 'send_handoff', { ...args, sends: 'title' }))).toBe('409 links.stale')
    expect((await sign(s, 'send_handoff', args)).message).toMatch(/Handed off DEMO-0045 to INT/)
    expect(await fail(sign(s, 'send_handoff', args))).toBe('409 links.already_sent')
    expect(s.store.eventsOf('DEMO-0045').at(-1)).toMatchObject({ type: 'links.sent', actor: { kind: 'addon', id: 'links' } })
    const tom = setup('p_tom')
    expect(await fail(sign(tom, 'send_handoff', args))).toBe('403 forbidden')
  })
  it('revoke is an owner\'s signature over the link and its peer; it closes requests, brings waiting handoffs back and stops the link', async () => {
    const m = setup('p_mara')
    expect(await fail(sign(m, 'revoke', { id: 'ln_int', peer: 'INT · Internal' }))).toBe('403 forbidden')
    const s = setup()
    expect(await fail(run(s, 'revoke', { id: 'ln_int', peer: 'INT · Internal' }))).toBe('409 confirm.required')
    expect(await fail(sign(s, 'revoke', { id: 'ln_int', peer: 'Northwind Grid · OPS' }))).toBe('409 links.stale')
    await sign(s, 'revoke', { id: 'ln_int', peer: 'INT · Internal' })
    expect(raw(s).requests.filter((r) => r.id.startsWith('rq_int')).map((r) => r.state)).toEqual(['closed', 'closed'])
    expect(raw(s).sent.find((x) => x.id === 'out_2')!.state).toBe('cancelled')
    expect(await fail(sign(s, 'send_handoff', { link: 'ln_int', ticket_key: 'DEMO-0045' }))).toBe('409 links.revoked')
    expect((await decisions(s)).some((d) => d.id.includes('rq_int'))).toBe(false)
    const evs = s.store.wsEventsOf(s.ws)
    expect(evs.find((e) => e.type === 'links.revoked')).toMatchObject({ actor: { kind: 'addon', id: 'links' } })
    expect(evs.find((e) => e.type === 'addon.action_signed' && e.action === 'revoke')).toMatchObject({ args: { id: 'ln_int', peer: 'INT · Internal' }, presence: 'touchid' })
  })
})
