import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '@/api/client'
import { FULL_ACCESS_WORDS, hubChip, type HubStatus } from './hub'
import { mockHubStatus, setHubScenario } from '@/mocks/hub'

const base = mockHubStatus('standalone')
const status = (over: Partial<HubStatus>): HubStatus => ({ ...mockHubStatus('selective'), ...over })

describe('hubChip (A5.1)', () => {
  it('is grey and quiet when standalone', () => {
    expect(hubChip(base)).toMatchObject({ tone: 'neutral', label: 'Local only', icon: 'none' })
  })
  it('is violet for selective and solid violet for full, saying what the hub can read', () => {
    expect(hubChip(status({ mode: 'selective' }))).toMatchObject({ tone: 'violet', label: 'Hub sees only what you share', icon: 'eye-off' })
    expect(hubChip(status({ mode: 'full', member: true }))).toMatchObject({ tone: 'violet-solid', label: 'Hub can read everything', icon: 'eye' })
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
      expect((await api.hubStatus('ws')).mode).toBe(mode)
    }
    setHubScenario('offline')
    expect((await api.hubStatus('ws')).reachable).toBe(false)
    setHubScenario('nottold')
    expect(await api.hubStatus('ws')).toMatchObject({ not_told: true, owed: 1 })
  })

  it('shares nothing without a hub and lists the shared tickets in selective mode', async () => {
    setHubScenario('standalone')
    expect(await api.hubShare('ws')).toMatchObject({ rules: [], tickets: [] })
    setHubScenario('selective')
    const share = await api.hubShare('ws')
    expect(share.tickets.length).toBeGreaterThan(0)
    expect(share.rules.filter((r) => r.kind === 'needs_you' || r.kind === 'questions')).toHaveLength(2)
  })

  it('previews one rule change without changing anything', async () => {
    setHubScenario('selective')
    const before = await api.hubShare('ws')
    const p = await api.hubPreview('ws', { add: { kind: 'tag', value: 'team' } })
    expect(p.new).toBe(1)
    expect(await api.hubShare('ws')).toEqual(before)
  })

  it('builds Terminal commands and never a hub write of its own', async () => {
    const join = await api.hubCommand('ws', { kind: 'join', invite: 'INV-7QK2-M9XD', card: 'hub card.json', mode: 'selective' })
    expect(join.command).toBe("orch hub join INV-7QK2-M9XD --hub-card 'hub card.json' --mode selective")
    expect((await api.hubCommand('ws', { kind: 'mode_full', card: 'c.json' })).command).toBe('orch hub mode full --hub-card c.json')
    expect((await api.hubCommand('ws', { kind: 'leave' })).command).toBe('orch hub leave')
    expect((await api.hubCommand('ws', { kind: 'share_remove', rule: { kind: 'ticket', value: 'DEMO-0045' } })).command).toBe('orch hub share rule remove --ticket DEMO-0045')
  })

  it('types the words for the one-way switch exactly', () => {
    expect(FULL_ACCESS_WORDS).toBe('full access')
  })
})
