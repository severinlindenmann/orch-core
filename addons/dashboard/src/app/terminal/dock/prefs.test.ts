import { afterEach, describe, expect, it, vi } from 'vitest'
import { clampDock, DEFAULT_PREFS, dockMax, readDockPrefs, rightFits, writeDockPrefs } from './prefs'

const view = { width: 1440, height: 900 }
afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('dock size limits', () => {
  it('bottom: 160 px to 70% of the height; right: 320 px to 60% of the width, keeping the page at 640 px or more', () => {
    expect(clampDock('bottom', 10, view)).toBe(160)
    expect(clampDock('bottom', 5000, view)).toBe(630)
    expect(clampDock('right', 10, view)).toBe(320)
    expect(clampDock('right', 5000, view)).toBe(800) // 1440 - 640 (the page keeps 640 px)
    expect(clampDock('right', 5000, view, 1208)).toBe(568) // a 232 px sidebar leaves 1208 for page + dock
    expect(dockMax('right', { width: 2400, height: 900 }, 2300)).toBe(1440) // 60% of the window is the cap
    expect(rightFits({ width: 1024, height: 768 }, 968)).toBe(false) // icon sidebar: 968 - 640 = 328 < 400: bottom
    expect(rightFits({ width: 1440, height: 900 }, 1208)).toBe(true) // 568 px of room
    expect(rightFits({ width: 1024, height: 768 }, 792)).toBe(false) // wide sidebar: 152 px is too little
    expect(clampDock('right', Number.NaN, view)).toBe(DEFAULT_PREFS.right)
  })
})

describe('dock prefs storage', () => {
  it('round-trips per viewer', () => {
    writeDockPrefs('p_sev', { side: 'right', open: true, bottom: 300, right: 500, harness: 'codex' })
    expect(readDockPrefs('p_sev')).toEqual({ side: 'right', open: true, bottom: 300, right: 500, harness: 'codex' })
    expect(readDockPrefs('p_mara')).toEqual(DEFAULT_PREFS)
  })
  it('falls back to the defaults on garbage or when storage throws', () => {
    localStorage.setItem('orch.dock.p_sev', '{nope')
    expect(readDockPrefs('p_sev')).toEqual(DEFAULT_PREFS)
    localStorage.setItem('orch.dock.p_sev', JSON.stringify({ side: 'left', open: 'yes', bottom: 'big' }))
    expect(readDockPrefs('p_sev')).toEqual(DEFAULT_PREFS)
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readDockPrefs('p_sev')).toEqual(DEFAULT_PREFS)
    expect(() => writeDockPrefs('p_sev', DEFAULT_PREFS)).not.toThrow()
  })
})
