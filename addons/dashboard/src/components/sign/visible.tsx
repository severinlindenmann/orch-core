// How core shows a string it did not write (an addon's id, key, value, option key, title) or a value that is signed:
// every character the person cannot see is made visible, so two different strings never look the same and nothing
// can reorder or hide the text around it.

/** Control and format characters (C0/C1, bidi overrides and isolates, zero-width marks, BOM) and line/paragraph separators. */
const HIDDEN = new RegExp('[\\p{Cc}\\p{Cf}\\u2028\\u2029]', 'gu')
/** Shown for a space at the start or end, where it would be invisible. */
export const EDGE_SPACE = '␠'

/**
 * The string with invisible characters as `\u{…}` escapes, leading/trailing spaces as ␠, and the empty string as `""`.
 * Pure text: the posted value is always the original string, never this.
 */
export function visible(s: string): string {
  if (s === '') return '""'
  const escaped = s.replace(HIDDEN, (c) => `\\u{${c.codePointAt(0)!.toString(16)}}`)
  const lead = /^ +/.exec(escaped)?.[0].length ?? 0
  const trail = lead === escaped.length ? 0 : (/ +$/.exec(escaped)?.[0].length ?? 0)
  return EDGE_SPACE.repeat(lead) + escaped.slice(lead, escaped.length - trail) + EDGE_SPACE.repeat(trail)
}

/**
 * `visible()` for a plain-text context (a dialog title, a cover sentence, a toast): anything beyond printable ASCII is
 * also wrapped in a first-strong isolate (U+2068 … U+2069), so right-to-left text cannot reorder the core words around it.
 */
export function plain(s: string): string {
  const v = visible(s)
  return /^[\x20-\x7e]*$/.test(v) ? v : `\u2068${v}\u2069`
}

/** The exact value, in mono, isolated (bdi) and with every invisible character shown. Never faded, never replaced. */
export const Raw = ({ children }: { children: string }) => (
  <bdi className="whitespace-pre-wrap break-all font-mono text-[12px] text-text [unicode-bidi:isolate]">{visible(children)}</bdi>
)
