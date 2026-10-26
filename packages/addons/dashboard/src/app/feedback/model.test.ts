import { describe, expect, it } from 'vitest'
import { buildReport, emptyState, imageName, makeZip, newItem, normalize, numbered, stamp, zipFiles, type FeedbackItem } from './model'

const PNG = 'data:image/png;base64,iVBORw0KGgo='
const state = () => {
  const s = emptyState()
  normalize(s)
  return s
}
const fill = (it: FeedbackItem, text: string, shots: string[] = []) => {
  it.text = text
  it.shots = shots
}

describe('feedback model', () => {
  it('keeps one empty field at the end and starts the round with the first content', () => {
    const s = state()
    expect(s.items).toHaveLength(1)
    expect(s.round).toBeNull()
    fill(s.items[0], 'first')
    normalize(s, new Date('2026-10-23T16:49:00.123Z'))
    expect(s.items).toHaveLength(2)
    expect(s.round).toBe('20261023T164900Z')
  })
  it('stamps UTC without separators', () => {
    expect(stamp(new Date('2026-01-02T03:04:05.678Z'))).toBe('20260102T030405Z')
  })
  it('numbers items and sub-items, skipping empty ones', () => {
    const s = state()
    fill(s.items[0], 'one')
    s.items[0].kids = [newItem(s), newItem(s), newItem(s)]
    fill(s.items[0].kids[0], 'sub a')
    fill(s.items[0].kids[2], 'sub b')
    normalize(s)
    fill(s.items[1], 'two')
    normalize(s)
    expect(numbered(s.items).map((r) => r.path)).toEqual(['1', '1.1', '1.2', '2'])
  })
  it('names images with the round so two rounds never collide, and 1.1 differs from the first image of 1', () => {
    expect(imageName('R', '1', 0)).toBe('R-fb1-img1.png')
    expect(imageName('R', '1.1', 0)).toBe('R-fb1.1-img1.png')
  })
  it('builds a sorted report that links each image', () => {
    const s = state()
    fill(s.items[0], 'dddd', [PNG])
    s.items[0].kids = [newItem(s)]
    fill(s.items[0].kids[0], 'sub', [PNG])
    s.round = '20261023T164900Z'
    normalize(s)
    const text = buildReport(s, '~/Downloads/', false)
    expect(text).toContain('# Feedback report 20261023T164900Z (2 items)')
    expect(text).toContain('Images are in: ~/Downloads/')
    expect(text).toContain('## Feedback 1\ndddd\n![20261023T164900Z-fb1-img1.png](~/Downloads/20261023T164900Z-fb1-img1.png)')
    expect(text).toContain('  ## Feedback 1.1\n  sub\n  ![20261023T164900Z-fb1.1-img1.png]')
    expect(buildReport(emptyState(), '', true)).toBe('')
  })
  it('puts FEEDBACK.md and images/ in the ZIP with relative links', async () => {
    const s = state()
    fill(s.items[0], 'x', [PNG])
    normalize(s, new Date('2026-10-23T16:49:00Z'))
    const files = zipFiles(s)
    expect(files.map((f) => f.name)).toEqual(['FEEDBACK.md', 'images/20261023T164900Z-fb1-img1.png'])
    expect(new TextDecoder().decode(files[0].data)).toContain('(images/20261023T164900Z-fb1-img1.png)')
    const bytes = new Uint8Array(await makeZip(files).arrayBuffer())
    expect([...bytes.slice(0, 4)]).toEqual([0x50, 0x4b, 3, 4])
    expect([...bytes.slice(-22, -18)]).toEqual([0x50, 0x4b, 5, 6])
  })
})
