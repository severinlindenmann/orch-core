// WYSIWYS: a dialog that has nothing to fill in prepares the request when it opens and shows the host's content; it never
// offers a Sign button first. Every caller of SignPopover either sets `autoStart` or is a form (`confirmLabel`), whose
// button says the form's own verb and whose content is what the person typed.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n)
    return statSync(p).isDirectory() ? files(p) : /\.tsx$/.test(n) && !/\.test\./.test(n) ? [p] : []
  })
}

/** The attribute text of each `<SignPopover ...>` opening tag in a source file. */
function tags(src: string): string[] {
  const out: string[] = []
  for (const m of src.matchAll(/<SignPopover\b/g)) {
    let depth = 0
    let j = (m.index ?? 0) + m[0].length
    for (; j < src.length; j++) {
      const c = src[j]
      if (c === '{') depth++
      else if (c === '}') depth--
      else if (c === '>' && depth === 0 && src[j - 1] !== '=') break
    }
    out.push(src.slice((m.index ?? 0) + m[0].length, j))
  }
  return out
}

describe('every SignPopover caller prepares on open or is a form', () => {
  const all = files(join(__dirname, '..', '..')).flatMap((f) => tags(readFileSync(f, 'utf8')).map((t) => ({ f, t })))
  it('finds the callers', () => expect(all.length).toBeGreaterThan(10))
  it.each(all.map((x, i) => [x.f.split('/src/')[1] + '#' + i, x.t]))('%s', (_name, tag) => {
    expect(/\bautoStart\b/.test(tag) || /\bconfirmLabel=/.test(tag)).toBe(true)
  })
})
