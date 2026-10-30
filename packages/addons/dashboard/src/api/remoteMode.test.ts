import { afterEach, describe, expect, it } from 'vitest'
import { hasFeature, inRemoteSession, setFeatures } from './features'
import { REMOTE_HIDDEN_FEATURES, REMOTE_HIDDEN_SETTINGS_TABS } from './remoteMode'
import { knownSettingsTab, settingsTabs } from '@/app/pages/settings/tabs'

const ALL = ['board', 'settings', 'market', 'remote', 'publish', 'worktrees', 'hub', 'agents', 'actions']
const open = (login: string) => setFeatures({ features: ALL, workspaces: { w: ALL }, login })

afterEach(() => setFeatures(null))

describe('remote mode', () => {
  it('is on only for a session opened through a stream', () => {
    open('remote-device')
    expect(inRemoteSession()).toBe(true)
    for (const l of ['local', 'remote', 'unknown']) { open(l); expect(inRemoteSession()).toBe(false) }
    setFeatures(null)
    expect(inRemoteSession()).toBe(false)
  })

  it('hides every local-only feature, in the list and per workspace, and keeps the rest', () => {
    open('remote-device')
    for (const f of REMOTE_HIDDEN_FEATURES) { expect(hasFeature(f)).toBe(false); expect(hasFeature(f, 'w')).toBe(false) }
    for (const f of ['board', 'agents', 'actions', 'settings']) expect(hasFeature(f)).toBe(true)
  })

  it('shows all of them on this computer', () => {
    open('local')
    for (const f of REMOTE_HIDDEN_FEATURES) expect(hasFeature(f)).toBe(true)
  })

  it('draws no settings tab that changes local things, and redirects a deep link to one', () => {
    open('remote-device')
    const ids = settingsTabs('w').map((t) => t.id)
    for (const t of REMOTE_HIDDEN_SETTINGS_TABS) { expect(ids).not.toContain(t); expect(knownSettingsTab(t)).toBe(false) }
    expect(ids).toContain('general')
    open('local')
    expect(settingsTabs('w').map((t) => t.id)).toContain('devices')
    expect(knownSettingsTab('devices')).toBe(true)
  })
})
