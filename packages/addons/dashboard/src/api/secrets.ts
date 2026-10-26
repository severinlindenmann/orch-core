// The secrets file (D56, kind B): `NAME=value` lines and `#` comments, nothing else. orch PARSES it: no shell
// sourcing, no expansion, no command substitution, no `export`, no quotes handling beyond taking the text as it is.
// And the output filter: known secret values are replaced before text reaches logs, events or transcripts.
import { ENV_RE } from './connections'

/** Shorter values are refused by the parser and never filtered (masking them would hide ordinary words). */
export const MIN_SECRET = 8

export interface ParsedSecrets {
  entries: { name: string; value: string; line: number }[]
  /** Refused lines: number and reason only (a refused line may hold a secret; its text is never repeated). */
  problems: { line: number; reason: string }[]
}

export function parseSecretsFile(text: string): ParsedSecrets {
  const entries: ParsedSecrets['entries'] = []
  const problems: ParsedSecrets['problems'] = []
  const seen = new Set<string>()
  text.split(/\r?\n/).forEach((raw, i) => {
    const n = i + 1
    const l = raw.trim()
    if (!l || l.startsWith('#')) return
    const eq = l.indexOf('=')
    if (eq < 0) return problems.push({ line: n, reason: 'not NAME=value' })
    const key = l.slice(0, eq)
    const value = l.slice(eq + 1)
    if (/^export\s/.test(key)) return problems.push({ line: n, reason: '"export" is not allowed: the file is parsed, not sourced' })
    if (!ENV_RE.test(key)) return problems.push({ line: n, reason: 'the name must be upper case letters, digits and _' })
    if (seen.has(key)) return problems.push({ line: n, reason: `${key} is set twice; the first one counts` })
    if (!value) return problems.push({ line: n, reason: `${key} has no value` })
    if (value.length < MIN_SECRET) return problems.push({ line: n, reason: `${key} is shorter than ${MIN_SECRET} characters: output filtering could not hide it` })
    seen.add(key)
    // Taken literally: "$HOME" stays the five characters $HOME, `$(cmd)` is never run.
    entries.push({ name: key, value, line: n })
  })
  return { entries, problems }
}

/** How a filtered secret value reads in output: `•••• (DATABRICKS_TOKEN)`. */
export const maskOf = (name: string) => `•••• (${name})`


/**
 * Replace every occurrence of a known secret value with its mask. Longer values first, so a value that contains
 * another is masked whole. Plain string search: values are never turned into patterns.
 */
export function maskSecrets(text: string, secrets: { name: string; value: string }[]): string {
  let out = text
  for (const s of [...secrets].filter((x) => x.value.length >= MIN_SECRET).sort((a, b) => b.value.length - a.value.length)) out = out.split(s.value).join(maskOf(s.name))
  return out
}
