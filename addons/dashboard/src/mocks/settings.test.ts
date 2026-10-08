import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { ApiError } from '@/api/types'
import { createMockStore } from './store'

function setup(viewer = 'p_sev') {
  const store = createMockStore({ persist: false })
  store.setViewer(viewer)
  const api = createApi(createMockTransport(store, { latency: false }))
  return { store, api, ws: store.workspaces[0].id }
}
const status = async (p: Promise<unknown>) => {
  try {
    await p
    return 200
  } catch (e) {
    return e instanceof ApiError ? e.status : -1
  }
}

describe('workspace settings API', () => {
  it('returns the identity', async () => {
    const { api, ws } = setup()
    const id = await api.getIdentity(ws)
    expect(id).toMatchObject({ uuid: ws, prefix: 'DEMO', epoch: 1 })
    expect(id.key_fingerprint).toMatch(/^SHA256:/)
  })
  it('refuses every op to non-owners with 403', async () => {
    const { api, ws } = setup('p_mara')
    expect(await status(api.postSettings(ws, { op: 'rename', name: 'X' }))).toBe(403)
    expect(await status(api.postSettings(ws, { op: 'member.role', person: 'p_tom', role: 'member' }))).toBe(403)
    expect(await status(api.postSettings(ws, { op: 'gate.policy', gate: 'plan', approvers: 'owner', count: 2 }))).toBe(403)
  })
  it('keeps the last owner, yourself and the owner role safe', async () => {
    const { api, ws } = setup()
    expect(await status(api.postSettings(ws, { op: 'member.role', person: 'p_sev', role: 'member' }))).toBe(409)
    expect(await status(api.postSettings(ws, { op: 'member.remove', person: 'p_sev' }))).toBe(409)
    expect(await status(api.postSettings(ws, { op: 'member.add', person: 'p_x', name: 'X', role: 'owner' }))).toBe(400)
  })
  it('validates gate counts and applies a policy', async () => {
    const { api, ws } = setup()
    expect(await status(api.postSettings(ws, { op: 'gate.policy', gate: 'plan', approvers: 'owner', count: 4 }))).toBe(400)
    const r = await api.postSettings(ws, { op: 'gate.policy', gate: 'plan', approvers: 'maintainer', count: 2, not: 'assignees' })
    expect(r.workspace.gates.plan).toEqual({ approvers: 'maintainer', count: 2, not: 'assignees' })
  })
  it('renames, adds and removes members', async () => {
    const { api, ws } = setup()
    expect((await api.postSettings(ws, { op: 'rename', name: 'Renamed' })).workspace.name).toBe('Renamed')
    const added = await api.postSettings(ws, { op: 'member.add', person: 'p_ida', name: 'Ida', role: 'member' })
    expect(added.workspace.members.find((m) => m.person === 'p_ida')?.role).toBe('member')
    expect((await api.postSettings(ws, { op: 'member.remove', person: 'p_ida' })).workspace.members.some((m) => m.person === 'p_ida')).toBe(false)
  })
})
