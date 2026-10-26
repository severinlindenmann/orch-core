import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, describe, expect, it } from 'vitest'
import { parseBlock, parseSection, resolveTicketWidgets } from './parse'

type Input = { raw?: string; text?: string; gen?: { prefix: string; unit: string; count: number; suffix: string } }
const expand = (c: Input) => c.gen ? c.gen.prefix + c.gen.unit.repeat(c.gen.count) + c.gen.suffix : (c.raw ?? c.text ?? '')
const path = join(dirname(fileURLToPath(import.meta.url)), 'parity-fixtures.json')
const fixtures = JSON.parse(readFileSync(path, 'utf8'))
const plain = (v: unknown) => JSON.parse(JSON.stringify(v))
const writing = process.env.WIDGETS_PARITY_WRITE === '1'

describe('shared widget parity', () => {
  for (const c of fixtures.blocks) it(`block: ${c.name}`, () => {
    const actual = plain(parseBlock(expand(c)))
    if (writing) c.result = actual
    else expect(actual).toEqual(c.result)
  })
  for (const c of fixtures.sections) it(`section: ${c.name}`, () => {
    const actual = plain(parseSection(expand(c), c.section))
    if (writing) c.segments = actual
    else expect(actual).toEqual(c.segments)
  })
  for (const c of fixtures.tickets) it(`ticket: ${c.name}`, () => {
    const actual = plain(resolveTicketWidgets(c.body, {
      order: c.order,
      label: c.labels ? (s: string) => c.labels[s] ?? s : undefined,
    }))
    if (writing) c.result = actual
    else expect(actual).toEqual(c.result)
  })
  // Never regenerate expected results during normal tests or CI.
  afterAll(() => {
    if (writing) writeFileSync(path, JSON.stringify(fixtures, null, 2) + '\n')
  })
})
