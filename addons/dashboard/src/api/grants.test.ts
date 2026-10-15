import { describe, expect, it } from 'vitest'
import { grantLabel } from './grants'

const NOW = Date.parse('2026-10-09T11:30:00Z')

describe('grantLabel', () => {
  it('names a grant by person, scope and end, never by id', () => {
    expect(grantLabel({ scope: 'all', until: '2026-10-09T17:00:00Z' }, 'Mara', NOW)).toBe("Mara's grant (all tickets · until 17:00)")
    expect(grantLabel({ scope: 'ci', until: '2026-10-10T09:00:00Z' }, 'Severin', NOW)).toBe("Severin's grant (CI only · until 10 Oct 09:00)")
  })
})
