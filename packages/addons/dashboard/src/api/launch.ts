// Agent launch rules shared by the host (mock) and addons (part of the API contract). A launch is structured — an
// argv and an environment — never a shell string assembled from settings; the string is only rendered for display.

/** A model name or alias: `opus`, `sonnet[1m]`, `claude-opus-5-5`, `vendor/model:tag`. No spaces, no leading "-". */
export const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:[\]/-]{0,63}$/
export const isModelName = (v: unknown): v is string => typeof v === 'string' && MODEL_NAME.test(v)

/** A ticket key: PREFIX-NUMBER. */
export const TICKET_KEY = /^[A-Z][A-Z0-9]{1,9}-\d{1,6}$/

/** What core runs: the program and its arguments (the prompt is ONE element) plus the extra environment. */
export interface LaunchSpec {
  argv: string[]
  env: Record<string, string>
}

const SAFE = /^[A-Za-z0-9_@%+=:,./-]+$/

/** POSIX shell quoting for display: safe words as they are, everything else in single quotes (' becomes '\''). */
export function shellQuote(s: string): string {
  if (s !== '' && SAFE.test(s)) return s
  return `'${s.replace(/'/g, `'\\''`)}'`
}

/** The command line a person could paste: `KEY=value ... argv...`, every part quoted. */
export function renderCommand(spec: LaunchSpec): string {
  const env = Object.entries(spec.env).map(([k, v]) => `${k}=${shellQuote(v)}`)
  return [...env, ...spec.argv.map(shellQuote)].join(' ')
}
