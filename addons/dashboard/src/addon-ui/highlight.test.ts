import { expect, it } from 'vitest'
import { highlight, langForFile, tokenClass } from './highlight'

it('colours a small file by token and maps colours to classes', async () => {
  const lines = await highlight('def f():\n    return 1\n', 'python')
  expect(lines).not.toBeNull()
  expect(lines!.flat().some((t) => tokenClass(t.color) === 'tok-keyword')).toBe(true)
})

it('falls back to plain text (null) when the total time budget is spent, however many lines remain', async () => {
  const pathological = Array.from({ length: 4000 }, (_, i) => `x${i} = "${'(a|a)*'.repeat(40)}" + ${'['.repeat(200)}`).join('\n')
  const started = performance.now()
  expect(await highlight(pathological, 'python', 0)).toBeNull()
  expect(await highlight(pathological, 'python', 30)).toBeNull()
  expect(performance.now() - started).toBeLessThan(3000)
})

it('a text over the line cap is not coloured', async () => {
  expect(await highlight('a\n'.repeat(5001), 'python')).toBeNull()
})

it('picks the language by file extension only', () => {
  expect(langForFile('check_json.py')).toBe('python')
  expect(langForFile('x.log')).toBeNull()
  expect(langForFile('script')).toBeNull()
})
