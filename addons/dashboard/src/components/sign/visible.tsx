// How core shows a string it did not write (an addon's id, key, value, option key, title) or a value that is signed:
// every character the person cannot see is made visible, so two different strings never look the same and nothing
// can reorder or hide the text around it. The encodings are injective (security review #8): each display decodes back
// to exactly one value (tested in visible.test.tsx).

/**
 * Characters shown as `\u{…}`: control and format characters (C0/C1, bidi overrides and isolates, zero-width marks,
 * BOM), every default-ignorable code point (U+034F, U+115F, U+3164, variation selectors, tags …), line/paragraph
 * separators, every space other than U+0020 (no-break, ideographic …), private-use, unassigned and lone surrogate
 * code points, and core's own edge-space marker ␠ (so a literal one never reads as a space).
 */
const HIDDEN = '\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}\\p{Default_Ignorable_Code_Point}\\p{Co}\\p{Cn}\\p{Cs}\\u2420'
const OTHER_SPACE = '(?! )\\p{Zs}'
const escape = (c: string) => `\\u{${c.codePointAt(0)!.toString(16)}}`
const pattern = (extra: string) => new RegExp(`\\\\|[${HIDDEN}]|${OTHER_SPACE}${extra}`, 'gu')
const STRICT = pattern('')
const QUOTED = pattern('|"')
const PROSE = new RegExp(`\\\\|(?!\\n)[${HIDDEN}]|${OTHER_SPACE}`, 'gu')

function encode(s: string, re: RegExp): string {
  const denormal = s !== s.normalize('NFC')
  const out = s.replace(re, (c) => (c === '\\' ? '\\\\' : c === '"' ? '\\"' : escape(c)))
  if (!denormal) return out
  // A string NFC would change can look like a different one (é vs e + U+0301, 가 vs its two jamo): every code point
  // beyond ASCII is then shown by number (round 2 #3). ASCII never changes under NFC, and escapes are ASCII.
  return [...out].map((c) => (c.codePointAt(0)! > 0x7e ? escape(c) : c)).join('')
}

/** Shown for a space at the start or end, where it would be invisible. */
export const EDGE_SPACE = '␠'

/**
 * The string with invisible characters as `\u{…}` escapes, a backslash as `\\`, leading/trailing spaces as ␠, the empty
 * string as `""` (and a string that is exactly two quote characters as `\"\"`). Pure text: the posted value is always
 * the original string, never this.
 */
export function visible(s: string): string {
  if (s === '') return '""'
  if (s === '""') return '\\"\\"'
  const escaped = encode(s, STRICT)
  const lead = /^ +/.exec(escaped)?.[0].length ?? 0
  const trail = lead === escaped.length ? 0 : (/ +$/.exec(escaped)?.[0].length ?? 0)
  return EDGE_SPACE.repeat(lead) + escaped.slice(lead, escaped.length - trail) + EDGE_SPACE.repeat(trail)
}

/**
 * A typed signed value (a signed arg, a decision term): a string in double quotes with `\"` and `\\` escaped and
 * invisible characters as `\u{…}`; a number or boolean bare. So `"1"` and `1`, `"true"` and `true` never read alike.
 */
export function visibleValue(v: unknown): string {
  if (typeof v === 'string') return `"${encode(v, QUOTED)}"`
  if (typeof v === 'number') return Object.is(v, -0) ? '-0' : String(v)
  if (typeof v === 'boolean') return String(v)
  return v === null ? 'null' : `(${typeof v})`
}

/**
 * Text a person reads as text (a plan, a question, an addon's sentence): the same escapes as `visible()` except that
 * line breaks stay line breaks. Used for multi-line signed or addon-written prose, always inside a bidi isolate.
 */
export function prose(s: string): string {
  return encode(s, PROSE)
}

/**
 * `visible()` for a plain-text context (a dialog title, a cover sentence, a toast): anything beyond printable ASCII is
 * also wrapped in a first-strong isolate (U+2068 … U+2069), so right-to-left text cannot reorder the core words around it.
 */
export function plain(s: string): string {
  const v = visible(s)
  return /^[\x20-\x7e]*$/.test(v) ? v : `⁨${v}⁩`
}

/** The exact value, in mono, isolated (bdi) and with every invisible character shown. Never faded, never replaced. */
export const Raw = ({ children }: { children: string }) => (
  <bdi className="whitespace-pre-wrap break-all font-mono text-[12px] text-text [unicode-bidi:isolate]">{visible(children)}</bdi>
)

/** A typed signed value (`visibleValue`): strings quoted, numbers and booleans bare; isolated like `Raw`. */
export const RawValue = ({ value }: { value: unknown }) => (
  <bdi data-value-type={typeof value} className="whitespace-pre-wrap break-all font-mono text-[12px] text-text [unicode-bidi:isolate]">
    {visibleValue(value)}
  </bdi>
)

/** Multi-line text from someone other than core, shown exactly (`prose`): isolated, wrapped, line breaks kept. */
export const Prose = ({ children, className, inline }: { children: string; className?: string; inline?: boolean }) => (
  <bdi className={`${inline ? 'inline' : 'block'} whitespace-pre-wrap [overflow-wrap:anywhere] [unicode-bidi:isolate] ${className ?? ''}`}>{prose(children)}</bdi>
)

/** One line of someone else's text in running core text (a task, a criterion, an option label): `visible()`, isolated, not mono. */
export const Inline = ({ children, className }: { children: string; className?: string }) => (
  <bdi className={`whitespace-pre-wrap [overflow-wrap:anywhere] [unicode-bidi:isolate] ${className ?? ''}`}>{visible(children)}</bdi>
)
