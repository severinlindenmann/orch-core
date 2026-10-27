import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '@/api/client'
import { hubCardId, hubChip, type HubStatus } from './hub'
import { mockHubStatus, setHubScenario } from '@/mocks/hub'

const base = mockHubStatus('standalone')
const status = (over: Partial<HubStatus>): HubStatus => ({ ...mockHubStatus('selective'), ...over })

describe('hubChip (A5.1)', () => {
  it('is grey and quiet when standalone', () => {
    expect(hubChip(base)).toMatchObject({ tone: 'neutral', label: 'Local only', icon: 'none' })
  })
  it('is quiet grey for selective and full: online, connected (the access level lives in Settings)', () => {
    expect(hubChip(status({ mode: 'selective' }))).toMatchObject({ tone: 'neutral', label: 'Online · connected to hub', icon: 'dot' })
    expect(hubChip(status({ mode: 'full', member: true }))).toMatchObject({ tone: 'neutral', label: 'Online · connected to hub', icon: 'dot' })
  })
  it('is amber when the hub is offline, not told, or needs attention', () => {
    expect(hubChip(status({ reachable: false }))?.tone).toBe('amber')
    expect(hubChip(mockHubStatus('nottold'))).toMatchObject({ tone: 'amber', short: 'Hub not told' })
    expect(hubChip(status({ reseal_needed: true }))?.tone).toBe('amber')
    expect(hubChip(status({ key_change_pending: true }))?.tone).toBe('amber')
  })
  it('never calls an unknown mode safe', () => {
    expect(hubChip(status({ mode: 'unknown' }))).toMatchObject({ tone: 'amber', label: 'Hub mode not known' })
  })
  it('shows nothing before the host answered', () => {
    expect(hubChip(null)).toBeNull()
    expect(hubChip(undefined)).toBeNull()
  })
  it('an unreachable standalone workspace is still just local', () => {
    expect(hubChip(status({ mode: 'standalone', reachable: false }))?.label).toBe('Local only')
  })
})

describe('the mock hub (what the pages are built against)', () => {
  beforeEach(() => setHubScenario(null))

  it('answers each scenario with the status the chip expects', async () => {
    for (const [scenario, mode] of [['standalone', 'standalone'], ['selective', 'selective'], ['full', 'full'], ['offline', 'selective']] as const) {
      setHubScenario(scenario)
      expect((await api.getHubStatus('ws')).mode).toBe(mode)
    }
    setHubScenario('offline')
    expect((await api.getHubStatus('ws')).reachable).toBe(false)
    setHubScenario('nottold')
    expect(await api.getHubStatus('ws')).toMatchObject({ not_told: true, owed: 1 })
  })

  it('shares nothing without a hub and lists the shared tickets in selective mode', async () => {
    setHubScenario('standalone')
    expect(await api.getHubShare('ws')).toMatchObject({ rules: [], tickets: [] })
    setHubScenario('selective')
    const share = await api.getHubShare('ws')
    expect(share.tickets.length).toBeGreaterThan(0)
    expect(share.rules.filter((r) => r.kind === 'needs_you' || r.kind === 'questions')).toHaveLength(2)
  })

  it('previews one rule change without changing anything', async () => {
    setHubScenario('selective')
    const before = await api.getHubShare('ws')
    const p = await api.getHubPreview('ws', { add: { kind: 'tag', value: 'team' } })
    expect(p.new).toBe(1)
    expect(await api.getHubShare('ws')).toEqual(before)
  })

  it('builds Terminal commands and never a hub write of its own', async () => {
    const join = await api.getHubCommand('ws', { kind: 'join', invite: 'INV-7QK2-M9XD', card: 'hub card.json', mode: 'selective' })
    expect(join.command).toBe("orch hub join INV-7QK2-M9XD --hub-card 'hub card.json' --mode selective")
    expect((await api.getHubCommand('ws', { kind: 'mode_full', card: 'c.json' })).command).toBe('orch hub mode full --hub-card c.json')
    expect((await api.getHubCommand('ws', { kind: 'leave' })).command).toBe('orch hub leave')
    expect((await api.getHubCommand('ws', { kind: 'share_remove', rule: { kind: 'ticket', value: 'DEMO-0045' } })).command).toBe('orch hub share rule remove --ticket DEMO-0045')
  })

  it('reads the hub id of a pasted hub card, and nothing else', () => {
    const id = '9f3c1ab277de40c8aa11bb22cc33dd44'
    expect(hubCardId(JSON.stringify({ o: { hub_id: id }, sig: 'x' }))).toEqual({ id, short: '9f3c 1ab2 77de 40c8' })
    for (const bad of ['', 'not json', '[]', '{}', '{"o":{}}', '{"o":{"hub_id":"XYZ"}}', '{"hub_id":"' + id + '"}']) expect(hubCardId(bad)).toBeNull()
  })

  it('the hub mock is reached only from the mock router and tests, never from pages (so it is absent from the production bundle)', async () => {
    const { readdirSync, readFileSync, statSync } = await import('node:fs')
    const { join } = await import('node:path')
    const walk = (dir: string): string[] => readdirSync(dir).flatMap((n) => (statSync(join(dir, n)).isDirectory() ? walk(join(dir, n)) : [join(dir, n)]))
    const users = walk('src')
      .filter((f) => /\.(ts|tsx)$/.test(f) && !/\.test\.tsx?$/.test(f) && !f.startsWith('src/mocks/'))
      .filter((f) => /from '(@\/mocks\/hub|\.\.?\/mocks\/hub)'/.test(readFileSync(f, 'utf8')))
    expect(users).toEqual([])
  })
})
