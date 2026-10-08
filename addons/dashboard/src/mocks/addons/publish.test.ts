import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { selectContributions } from '@/addon-ui/slots'

const setup = () => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type App = { id: string; name: string; kind: string; status: string; recipients: number; log?: string[] }
type Share = { id: string; ticket?: string; kind: string; title: string; views: number; expires_in_days: number; last_viewer?: string }
const state = async (s: ReturnType<typeof setup>) => (await s.api.getAddonState(s.ws, 'publish')) as Record<string, unknown> & { apps: App[]; shares: Share[] }

describe('publish seed', () => {
  it('has three apps, five shares and settings', async () => {
    const s = setup()
    const st = await state(s)
    expect(st.apps.map((a) => [a.name, a.kind, a.status])).toEqual([
      ['Billing explorer', 'streamlit', 'running'],
      ['Energy dashboard', 'static', 'stopped'],
      ['Ops notebook', 'notebook', 'failed'],
    ])
    expect(st.apps[0].recipients).toBe(2)
    expect(st.apps[2].log?.length).toBeGreaterThan(2)
    expect(st.shares).toHaveLength(5)
    expect(new Set(st.shares.map((x) => x.kind))).toEqual(new Set(['public link', 'secret link', 'sealed']))
    expect(st.shares.every((x) => x.views >= 0 && x.expires_in_days > 0)).toBe(true)
    expect(st.settings).toMatchObject({ default_expiry_days: 7, namespace: 'acme' })
  })
  it('derives the today summary from state', async () => {
    const s = setup()
    expect((await state(s)).summary).toBe('1 app running · 1 failed build')
    await s.api.runAddonAction(s.ws, 'publish', 'start', { id: 'app_energy' })
    expect((await state(s)).summary).toBe('2 apps running · 1 failed build')
  })
})

describe('publish app actions', () => {
  it('start flips a stopped app to running, stop flips it back', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'publish', 'start', { id: 'app_energy' })
    expect((await state(s)).apps.find((a) => a.id === 'app_energy')!.status).toBe('running')
    await s.api.runAddonAction(s.ws, 'publish', 'stop', { id: 'app_energy' })
    expect((await state(s)).apps.find((a) => a.id === 'app_energy')!.status).toBe('stopped')
  })
  it('logs returns the last lines; redeploy rebuilds a failed app', async () => {
    const s = setup()
    const logs = await s.api.runAddonAction(s.ws, 'publish', 'logs', { id: 'app_ops' })
    expect(logs.message).toContain('ModuleNotFoundError')
    await s.api.runAddonAction(s.ws, 'publish', 'redeploy', { id: 'app_ops' })
    expect((await state(s)).apps.find((a) => a.id === 'app_ops')!.status).toBe('running')
    expect((await state(s)).summary).toBe('2 apps running · 0 failed builds')
  })
  it('refuses an unknown app without changing anything', async () => {
    const s = setup()
    const r = await s.api.runAddonAction(s.ws, 'publish', 'start', { id: 'nope' })
    expect(r.changed).toBeFalsy()
  })
  it('a viewer cannot start an app', async () => {
    const s = setup()
    s.store.setViewer('p_tom')
    await expect(s.api.runAddonAction(s.ws, 'publish', 'start', { id: 'app_energy' })).rejects.toMatchObject({ status: 403 })
  })
})

describe('publish shares', () => {
  it('share writes addon state only (no ticket addon data) and appears under the ticket', async () => {
    const s = setup()
    const r = await s.api.runAddonAction(s.ws, 'publish', 'share', { ticket: 'DEMO-0043' })
    expect(r.changed).toBe(true)
    const st = await state(s)
    expect(st.shares).toHaveLength(6)
    expect(st.shares[0]).toMatchObject({ ticket: 'DEMO-0043', kind: 'secret link', expires_in_days: 7 })
    expect((await s.api.getTicket('DEMO-0043')).addons.publish).toBeUndefined()
    expect(((st.sharesByTicket as Record<string, { title: string }[]>)['DEMO-0043'] ?? []).length).toBe(1)
    expect((await s.api.getTicket('DEMO-0041')).addons.publish).toBeUndefined()
  })
  it('revoke removes the share from state and from the ticket panel', async () => {
    const s = setup()
    const panel = async () => {
      const pkgs = await s.api.getAddons()
      const w = (await s.api.getWorkspaces()).find((x) => x.id === s.ws)!
      const t = await s.api.getTicket('DEMO-0041')
      const addon = await state(s)
      const c = selectContributions(pkgs, 'ticket.panel', { workspace: w, ticket: t, addon }).find((x) => x.addon === 'publish')!
      return JSON.stringify(c.node)
    }
    expect(await panel()).toContain('Before/after report')
    const sh = (await state(s)).shares.find((x) => x.title === 'Before/after report')!
    await s.api.runAddonAction(s.ws, 'publish', 'revoke', { id: sh.id })
    expect((await state(s)).shares.find((x) => x.id === sh.id)).toBeUndefined()
    expect(await panel()).not.toContain('Before/after report')
  })
  it('extend adds 7 days; copy_link returns the link', async () => {
    const s = setup()
    const sh = (await state(s)).shares.find((x) => x.kind === 'secret link')!
    const before = sh.expires_in_days
    await s.api.runAddonAction(s.ws, 'publish', 'extend', { id: sh.id })
    expect((await state(s)).shares.find((x) => x.id === sh.id)!.expires_in_days).toBe(before + 7)
    const c = await s.api.runAddonAction(s.ws, 'publish', 'copy_link', { id: sh.id })
    expect(c.message).toMatch(/https:\/\/p\.acme\.example\/s\//)
  })
  it('share_once returns the full link in the message and keeps it hidden after', async () => {
    const s = setup()
    const r = await s.api.runAddonAction(s.ws, 'publish', 'share_once', { ticket: 'DEMO-0041' })
    expect(r.message).toMatch(/^Link copied, shown once: https:\/\/p\.acme\.example\/s\/[A-Za-z0-9]{8,}$/)
    const st = await state(s)
    const once = st.shares[0]
    expect(once.kind).toBe('show-once')
    expect(JSON.stringify(st)).not.toContain(r.message!.split(': ')[1])
    const again = await s.api.runAddonAction(s.ws, 'publish', 'copy_link', { id: once.id })
    expect(again.message).toMatch(/shown once/i)
    expect(again.message).not.toMatch(/https:/)
  })
})

describe('publish decisions', () => {
  const ids = async (s: ReturnType<typeof setup>) => (await s.api.getAddonDecisions(s.ws)).filter((d) => d.addon === 'publish').map((d) => d.id)
  it('shows the failed-build decision to Severin, not to Tom', async () => {
    const s = setup()
    expect(await ids(s)).toEqual(expect.arrayContaining(['dec_publish_report', 'dec_publish_failed_build']))
    s.store.setViewer('p_tom')
    expect(await ids(s)).toEqual([])
  })
  it('deciding removes it, and retry rebuilds the app', async () => {
    const s = setup()
    const r = await s.api.runAddonAction(s.ws, 'publish', 'decide', { id: 'dec_publish_failed_build', option: 'retry' })
    expect(r.changed).toBe(true)
    expect(await ids(s)).toEqual(['dec_publish_report'])
    expect((await state(s)).apps.find((a) => a.id === 'app_ops')!.status).toBe('running')
  })
  it('"not now" removes the decision and leaves the app failed', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'publish', 'decide', { id: 'dec_publish_failed_build', option: 'no' })
    expect(await ids(s)).toEqual(['dec_publish_report'])
    expect((await state(s)).apps.find((a) => a.id === 'app_ops')!.status).toBe('failed')
  })
  it('publishing the report creates a share on its ticket', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'publish', 'decide', { id: 'dec_publish_report', option: 'yes', ticket: 'DEMO-0041' })
    expect(await ids(s)).toEqual(['dec_publish_failed_build'])
    expect((await state(s)).shares.some((x) => x.ticket === 'DEMO-0041' && x.title === 'Before/after report' && x.id !== 'sh_report')).toBe(true)
  })
  it('does not remove the package decisions (state-driven, package untouched)', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'publish', 'decide', { id: 'dec_publish_failed_build', option: 'no' })
    expect(s.store.addons.find((a) => a.name === 'publish')!.decisions!.map((d) => d.id)).toContain('dec_publish_failed_build')
  })
})

describe('closed decisions are refused by the store, for any addon', () => {
  it('deciding the same decision twice is refused the second time and does nothing', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'publish', 'decide', { id: 'dec_publish_report', option: 'yes', ticket: 'DEMO-0041' })
    const shares = (await state(s)).shares.length
    const again = await s.api.runAddonAction(s.ws, 'publish', 'decide', { id: 'dec_publish_report', option: 'yes', ticket: 'DEMO-0041' })
    expect(again).toMatchObject({ ok: true, message: 'That decision is closed.' })
    expect(again.changed).toBeFalsy()
    expect((await state(s)).shares).toHaveLength(shares)
  })
  it('a decision that is not currently open (failed-build once the app runs) is closed', async () => {
    const s = setup()
    await s.api.runAddonAction(s.ws, 'publish', 'redeploy', { id: 'app_ops' })
    const r = await s.api.runAddonAction(s.ws, 'publish', 'decide', { id: 'dec_publish_failed_build', option: 'retry' })
    expect(r.message).toBe('That decision is closed.')
  })
})

