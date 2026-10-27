import { describe, expect, it } from 'vitest'
import { queries } from './queries'

describe('perf: one request for the unfiltered ticket list', () => {
  it('the Tickets list without filters shares its cache entry with ticketsAll', () => {
    expect(queries.tickets('ws', {}).queryKey).toEqual(queries.ticketsAll('ws').queryKey)
    expect(queries.tickets('ws', { q: undefined }).queryKey).toEqual(queries.ticketsAll('ws').queryKey)
  })
  it('a filtered list has its own entry', () => {
    expect(queries.tickets('ws', { q: 'x' }).queryKey).not.toEqual(queries.ticketsAll('ws').queryKey)
  })
})
