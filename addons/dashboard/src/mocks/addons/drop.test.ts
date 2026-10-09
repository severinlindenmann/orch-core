import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'

// Drop (Preview, orch v2 P5): inbox (claim-once), sent drops sealed to a person / a workspace / a link, simulated upload.
const setup = (viewer = 'p_sev', install = true, dataset: 'normal' | 'busy' = 'normal') => {
  const store = createMockStore({ persist: false, dataset })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  if (install) installAndGrant(store, ws, 'drop')
  store.setViewer(viewer)
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
type S = ReturnType<typeof setup>
interface Row { id: string; file: string; state: string; ticket: string; canClaim?: boolean; canChange?: boolean; canExtend?: boolean; to?: string; views?: string }
interface State { inboxRows: Row[]; sentRows: Row[]; toClaim: number; sendToNames: string[]; shareSchema: { properties: { to: { enum: string[] }; ticket: { enum: string[] } } }; inbox: unknown[]; sent: unknown[] }
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'drop')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'drop', id, body)
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string }) => `${e.status} ${e.code}`)

describe('drop package', () => {
  it('starts in the catalog as a preview; minRoles live in the manifest; destructive acts are confirmed by core', () => {
    const { store, ws } = setup('p_sev', false)
    expect(store.workspaces.find((w) => w.id === ws)!.addons.drop).toBeUndefined()
    const pkg = store.addons.find((a) => a.name === 'drop')!
    expect(pkg.preview).toBe(true)
    expect(pkg.actions).toMatchObject({ share: { minRole: 'member' }, claim: { minRole: 'member' }, download: { minRole: 'viewer' }, revoke: { minRole: 'member', confirm: 'destructive' }, remove: { confirm: 'destructive' } })
    expect(pkg.contributions.filter((c) => c.slot === 'nav')).toHaveLength(1)
  })
  it('is refused while inactive', async () => {
    const s = setup('p_sev', false)
    expect(await fail(s.api.getAddonState(s.ws, 'drop'))).toBe('409 addon.inactive')
  })
})

describe('inbox', () => {
  it('lists received files newest first with sender, size, expiry and state', async () => {
    const st = await state(setup())
    expect(st.inboxRows[0]).toMatchObject({ file: 'meter-room-photos.zip', ticket: 'DEMO-0043', state: 'for this workspace' })
    expect(st.inboxRows.find((r) => r.file === 'release-notes.md')).toMatchObject({ state: 'to claim', canClaim: true })
    expect(st.toClaim).toBe(1)
  })
  it('claim-once: the first claim wins; a repeat is idempotent; a file for this workspace only cannot be claimed', async () => {
    const s = setup()
    expect((await run(s, 'claim', { id: 'in_notes' })).message).toMatch(/Claimed release-notes.md/)
    expect((await state(s)).inboxRows.find((r) => r.id === 'in_notes')).toMatchObject({ state: 'claimed here', canClaim: false })
    expect((await run(s, 'claim', { id: 'in_notes' })).message).toMatch(/already claimed here/)
    expect(await fail(run(s, 'claim', { id: 'in_photos' }))).toBe('409 drop.not_claimable')
    expect(await fail(run(s, 'claim', { id: 'nope' }))).toBe('404 not_found')
  })
  it('claim-once across workspaces: one store-wide claim; the sender (INT) never gets the file', async () => {
    const s = setup()
    const cli = s.store.workspaces.find((w) => w.prefix === 'CLI')!.id
    const int = s.store.workspaces.find((w) => w.prefix === 'INT')!.id
    installAndGrant(s.store, cli, 'drop')
    installAndGrant(s.store, int, 'drop')
    s.store.setViewer('p_sev')
    const rows = async (ws: string) => ((await s.api.getAddonState(ws, 'drop')) as unknown as State).inboxRows
    expect((await rows(int)).some((r) => r.id === 'in_notes')).toBe(false)
    expect((await rows(cli)).find((r) => r.id === 'in_notes')).toMatchObject({ state: 'to claim', canClaim: true })
    await run(s, 'claim', { id: 'in_notes' })
    expect((await rows(cli)).find((r) => r.id === 'in_notes')).toMatchObject({ state: 'claimed by DEMO', canClaim: false })
    expect(await fail(s.api.runAddonAction(cli, 'drop', 'claim', { id: 'in_notes' }))).toBe('409 drop.claimed')
    // The invoice went to DEMO and INT and DEMO claimed it before the demo.
    expect(await fail(s.api.runAddonAction(int, 'drop', 'claim', { id: 'in_invoice' }))).toBe('409 drop.claimed')
  })
  it('a viewer reads and downloads but cannot claim', async () => {
    const s = setup('p_tom')
    expect((await run(s, 'download', { id: 'in_notes' })).message).toMatch(/Simulated/)
    expect(await fail(run(s, 'claim', { id: 'in_notes' }))).toMatch(/^403/)
  })
  it('hides files tied to a ticket the viewer cannot see, and refuses actions on them as not found', async () => {
    const tom = setup('p_tom')
    const st = await state(tom)
    expect(st.inboxRows.some((r) => r.file === 'incident-timeline.md')).toBe(false)
    expect(JSON.stringify(st)).not.toContain('DEMO-0044')
    expect(await fail(run(tom, 'download', { id: 'in_private' }))).toBe('404 not_found')
    expect((await state(setup('p_mara'))).inboxRows.some((r) => r.file === 'incident-timeline.md')).toBe(true)
  })
})

describe('sharing a file', () => {
  it('sealed to a person: no link, and an event by the addon on the ticket', async () => {
    const s = setup()
    const r = await run(s, 'share', { formData: { file: 'release-notes.md', to: 'person:p_mara', days: 7, ticket: 'DEMO-0043' } })
    expect(r.message).toBe('Sealed release-notes.md to Mara. Only their devices can open it, for 7 days.')
    expect(r.secret).toBeUndefined()
    expect((await state(s)).sentRows[0]).toMatchObject({ file: 'release-notes.md', to: 'Sealed to Mara', state: 'live', ticket: 'DEMO-0043' })
    const e = s.store.eventsOf('DEMO-0043').at(-1)!
    expect(e).toMatchObject({ type: 'drop.shared', actor: { kind: 'addon', id: 'drop' } })
  })
  it('a link is handed out once in core\'s secret dialog and never stored in state', async () => {
    const s = setup()
    const r = await run(s, 'share', { formData: { file: 'tariff-export.csv', to: 'link', days: 1, max_views: 5 } })
    expect(r.secret!.value).toMatch(/^https:\/\/relay\.dev\.severin\.io\/d\/\d+#k=/)
    expect(r.secret!.note).toMatch(/shown once/)
    expect(r.secret!.note).toContain('Opens only in the orch app (iPhone or Mac). The relay has no download page')
    const st = await state(s)
    expect(JSON.stringify(st)).not.toContain(r.secret!.value.split('#k=')[1])
    expect(st.sentRows[0]).toMatchObject({ to: 'Link · orch app only · up to 5 views', views: '0 of 5' })
  })
  it('into another of your workspaces; the choices come from state', async () => {
    const s = setup()
    const st = await state(s)
    expect(st.shareSchema.properties.to.enum).toEqual(['person:p_mara', 'person:p_tom', 'workspace:INT', 'workspace:CLI', 'link'])
    expect((await run(s, 'share', { formData: { file: 'contract-scan.pdf', to: 'workspace:INT', days: 30 } })).message).toMatch(/into INT's inbox/)
  })
  it('refuses bad input with 4xx', async () => {
    const s = setup()
    expect(await fail(run(s, 'share', { formData: { file: 'x.exe', to: 'link', days: 7 } }))).toBe('400 validation')
    expect(await fail(run(s, 'share', { formData: { file: 'release-notes.md', to: 'person:p_sev', days: 7 } }))).toBe('400 validation')
    expect(await fail(run(s, 'share', { formData: { file: 'release-notes.md', to: 'link', days: 3 } }))).toBe('400 validation')
    expect(await fail(run(s, 'share', { formData: { file: 'release-notes.md', to: 'link', days: 7, max_views: 0 } }))).toBe('400 validation')
    const tom = setup('p_tom')
    expect(await fail(run(tom, 'share', { formData: { file: 'release-notes.md', to: 'link', days: 7 } }))).toMatch(/^403/)
  })
  it('a ticket of another workspace is not found (checked by the addon, like a hidden one); the ticket choices hide restricted ones', async () => {
    const s = setup()
    const other = s.store.ticketKeys(s.store.workspaces.find((w) => w.prefix === 'INT')!.id)[0]
    expect(await fail(run(s, 'share', { formData: { file: 'release-notes.md', to: 'link', days: 7, ticket: other } }))).toBe('404 not_found')
    expect((await state(setup('p_tom'))).shareSchema.properties.ticket.enum).not.toContain('DEMO-0044')
    const cli = setup('p_tom')
    const cliWs = cli.store.workspaces.find((w) => w.prefix === 'CLI')!.id
    installAndGrant(cli.store, cliWs, 'drop')
    cli.store.setViewer('p_tom')
    // Tom is a member in CLI; DEMO-0044 is restricted and of another workspace: not found either way.
    expect(await fail(cli.api.runAddonAction(cliWs, 'drop', 'share', { formData: { file: 'release-notes.md', to: 'link', days: 7, ticket: 'DEMO-0044' } }))).toBe('404 not_found')
    expect((await state(setup('p_mara'))).shareSchema.properties.ticket.enum).toContain('DEMO-0044')
  })
})

describe('sent drops', () => {
  it('only the sender or an owner extends; remove and extend write addon events', async () => {
    const mara = setup('p_mara')
    expect(await fail(run(mara, 'extend', { id: 'out_report' }))).toBe('403 forbidden') // Severin sent it
    expect((await run(mara, 'extend', { id: 'out_int' })).message).toMatch(/now expires/) // her own
    const sev = setup()
    expect((await run(sev, 'extend', { id: 'out_int' })).message).toMatch(/now expires/) // the owner
    expect((await run(sev, 'extend', { id: 'out_diff' })).ok).toBe(true)
    expect(sev.store.eventsOf('DEMO-0043').at(-1)).toMatchObject({ type: 'drop.extended', actor: { kind: 'addon', id: 'drop' } })
    await run(sev, 'remove', { id: 'in_photos' })
    expect(sev.store.eventsOf('DEMO-0043').at(-1)).toMatchObject({ type: 'drop.removed', actor: { kind: 'addon', id: 'drop' } })
  })
  it('Extend is offered only to the sender or an owner', async () => {
    const mara = (await state(setup('p_mara'))).sentRows
    expect(mara.find((r) => r.id === 'out_report')).toMatchObject({ canChange: true, canExtend: false }) // Severin sent it
    expect(mara.find((r) => r.id === 'out_int')?.canExtend).toBe(true) // her own
    const sev = (await state(setup())).sentRows
    expect(sev.filter((r) => r.canChange).every((r) => r.canExtend)).toBe(true) // the owner
  })
  it('extend and revoke; revoke is idempotent and adds an addon event on the ticket', async () => {
    const s = setup()
    expect((await run(s, 'extend', { id: 'out_report' })).message).toMatch(/now expires in 8 days/)
    expect((await run(s, 'revoke', { id: 'out_report' })).message).toMatch(/Revoked/)
    expect((await run(s, 'revoke', { id: 'out_report' })).message).toMatch(/already revoked/)
    expect(await fail(run(s, 'extend', { id: 'out_report' }))).toBe('409 drop.revoked')
    expect(s.store.eventsOf('DEMO-0041').at(-1)).toMatchObject({ type: 'drop.revoked', actor: { kind: 'addon', id: 'drop' } })
    expect((await state(s)).sentRows.find((r) => r.id === 'out_report')!.state).toBe('revoked')
    expect((await state(s)).sentRows.find((r) => r.id === 'out_old')!.state).toBe('expired')
  })
})

describe('busy day', () => {
  it('has a full inbox and many sent drops, the page state stays small', async () => {
    const s = setup('p_sev', true, 'busy')
    const st = await state(s)
    expect(st.inboxRows.length).toBeGreaterThan(30)
    expect(st.sentRows.length).toBeGreaterThan(25)
    expect(JSON.stringify(st).length).toBeLessThan(60_000)
  })
})
