import { describe, expect, it } from 'vitest'
import { railCollapsed, railToggle, type RailInputs } from './railRule'

const base: RailInputs = { pref: 'auto', dockPref: 'auto', windowWidth: 1440, squeezed: false }

describe('sidebar rail rule (N11)', () => {
  it('without a choice: wide from 1280 px, the rail below', () => {
    expect(railCollapsed(base)).toBe(false)
    expect(railCollapsed({ ...base, windowWidth: 1279 })).toBe(true)
  })

  it('switches to the rail when the right-hand dock squeezes the page, also over a usual "wide" choice', () => {
    expect(railCollapsed({ ...base, squeezed: true })).toBe(true)
    expect(railCollapsed({ ...base, pref: 'wide', squeezed: true })).toBe(true)
  })

  it('a choice made beside the dock wins there and is kept apart from the usual one', () => {
    expect(railCollapsed({ ...base, dockPref: 'wide', squeezed: true })).toBe(false)
    expect(railCollapsed({ ...base, dockPref: 'wide', pref: 'narrow', squeezed: true })).toBe(false)
    // Without the squeeze the usual choice applies again.
    expect(railCollapsed({ ...base, dockPref: 'wide', pref: 'narrow' })).toBe(true)
    expect(railCollapsed({ ...base, dockPref: 'narrow' })).toBe(false)
  })

  it('the usual choice still applies on its own', () => {
    expect(railCollapsed({ ...base, pref: 'narrow' })).toBe(true)
    expect(railCollapsed({ ...base, pref: 'wide', windowWidth: 1100 })).toBe(false)
  })

  it('a toggle records the choice for the situation it is made in', () => {
    expect(railToggle({ ...base, squeezed: true })).toEqual({ which: 'dockPref', value: 'wide' })
    expect(railToggle(base)).toEqual({ which: 'pref', value: 'narrow' })
    expect(railToggle({ ...base, windowWidth: 1100 })).toEqual({ which: 'pref', value: 'wide' })
  })
})
