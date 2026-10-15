// Helpers for strict checks over untrusted JSON (widget blocks, template data). They read own keys only and never
// rely on a prototype: strictJson builds prototype-free objects, and a key like "__proto__" is just an unknown key.

export type Obj = Record<string, unknown>

export const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v)

/** Own property test; never sees inherited names such as "toString". */
export const has = (o: Obj, k: string) => Object.prototype.hasOwnProperty.call(o, k)

/** The reason for the first key of `o` that is not in `allowed`, naming where it was found. */
export function unknownKey(o: Obj, allowed: readonly string[], where: string): string | undefined {
  for (const k of Object.keys(o)) if (!allowed.includes(k)) return `unknown key "${k}" in ${where}`
}
