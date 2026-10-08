import { describe, expect, it } from 'vitest'
import { actionMinRole, getAddon } from './index'

describe('addon registry minimum roles', () => {
  it('defaults to member and declares owner for save_settings, maintainer for decide', () => {
    for (const name of ['publish', 'github', 'usage', 'terminals', 'wiki', 'estimate']) {
      const a = getAddon(name)!
      expect(actionMinRole(a.actions.save_settings), `${name}.save_settings`).toBe('owner')
    }
    expect(actionMinRole(getAddon('estimate')!.actions.set)).toBe('member')
    expect(actionMinRole(getAddon('publish')!.actions.share)).toBe('member')
    expect(actionMinRole(getAddon('publish')!.actions.decide)).toBe('maintainer')
  })
})
