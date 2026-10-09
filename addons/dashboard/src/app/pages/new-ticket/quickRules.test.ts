import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockStore } from '@/mocks/store'
import { createMockTransport } from '@/api/transport'
import { guessType, quickProblem, quickRequest, quickTitle, SAMPLE_TRANSCRIPTS } from './quickRules'

describe('quick ticket rules', () => {
  it.each([
    ['Login button is broken on Safari', 'bug'],
    ['Fix the typo in the footer', 'bug'],
    ['Export fails on empty months', 'bug'],
    ['Investigate slow nightly import', 'spike'],
    ['Bump vite to 7', 'chore'],
    ['Refactor the board columns', 'chore'],
    ['Add a dark chart legend', 'feature'],
    ['Prefix handling for epics', 'feature'],
  ])('%s → %s', (text, type) => {
    expect(guessType(text)).toBe(type)
  })
  it('does not match parts of words', () => {
    expect(guessType('Prefixes and debugger panel')).toBe('feature')
  })
  it('titles from the first sentence and cuts long ones at a word', () => {
    expect(quickTitle('Export is broken. It should work.')).toBe('Export is broken')
    expect(quickTitle('  First line\nsecond line')).toBe('First line')
    const long = quickTitle('word '.repeat(40))
    expect(long.length).toBeLessThanOrEqual(80)
    expect(long.endsWith('…')).toBe(true)
  })
  it('refuses text too short for a title', () => {
    expect(quickProblem('ok')).toMatch(/few words/)
    expect(quickProblem('Do it')).toBeNull()
  })
  it('every sample transcription is accepted by the host as a backlog ticket', async () => {
    const store = createMockStore({ persist: false })
    const api = createApi(createMockTransport(store, { latency: false }))
    const ws = store.workspaces[0].id
    for (const text of SAMPLE_TRANSCRIPTS) {
      const { ticket } = await api.createTicket(ws, quickRequest(text))
      expect(ticket.status).toBe('backlog')
      expect(ticket.body.requirements).toBe(text)
    }
  })
})
