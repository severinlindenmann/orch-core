import { expect, it } from 'vitest'
import { Terminal } from '@xterm/xterm'
import type { Run } from '@/api/runs'
import { transcript, transcriptName } from './transcript'

it('reads only buffer lines, preserving inner empty lines and trimming trailing empty lines', async () => {
  const term = new Terminal()
  await new Promise<void>((resolve) => term.write('first\r\n\r\n  last  \r\n\r\n', resolve))
  expect(transcript(term)).toBe('first\n\n  last  ')
  term.dispose()
})
it('joins soft-wrapped rows into one line', async () => {
  const term = new Terminal({ cols: 10 })
  await new Promise<void>((resolve) => term.write('abcdefghijklmnopqrstuvwxy\r\nnext\r\n', resolve))
  expect(transcript(term)).toBe('abcdefghijklmnopqrstuvwxy\nnext')
  term.dispose()
})
it('names transcripts with a bounded slug and local date and time', () => {
  const run = (label: string) => ({ label } as Run)
  const now = new Date(2026, 0, 2, 3, 4)
  expect(transcriptName(run(' /My SHELL!! '), now)).toBe('my-shell-20260102-0304.txt')
  expect(transcriptName(run('!!!'), now)).toBe('terminal-20260102-0304.txt')
  expect(transcriptName(run('A'.repeat(60)), now)).toBe(`${'a'.repeat(40)}-20260102-0304.txt`)
  expect(transcriptName({ harness: 'shell', ticket: 'DEMO-0001' } as Run, now)).toBe('demo-0001-shell-20260102-0304.txt')
})
