import { z } from 'zod'
import { RESERVED_KEYS } from './actionRuntime'

const word = z.string().min(1).max(80)
const value = z.union([z.string().min(1).max(60), z.number().finite()])
const field = z.object({
  // A choice is posted as an arg under its key: a key core sets itself (ticket, confirmed, ...) would be stripped or
  // would collide, so the dialog would show a choice that is never sent. Refused.
  key: z.string().regex(/^[A-Za-z][A-Za-z0-9_]{0,31}$/).refine((k) => !RESERVED_KEYS.includes(k), 'reserved key'),
  label: word,
  choices: z.array(z.object({ value, label: word })).min(1).max(20),
  default: value,
})
const options = z
  .object({ note: z.string().max(200).optional(), fields: z.array(field).min(1).max(6) })
  .refine((o) => new Set(o.fields.map((f) => f.key)).size === o.fields.length, 'duplicate field keys')
  .refine((o) => o.fields.every((f) => new Set(f.choices.map((c) => String(c.value))).size === f.choices.length), 'duplicate choices')

export type ParsedOptions = z.output<typeof options>

/**
 * Core's own check of a manifest's `options` (the package is untrusted): null when it is not usable, so the action is
 * refused rather than posted. A `default` that is not one of the choices becomes the first choice, so the dialog never
 * shows one value and posts another.
 */
export function parseOptions(raw: unknown): ParsedOptions | null {
  const r = options.safeParse(raw)
  if (!r.success) return null
  return { ...r.data, fields: r.data.fields.map((f) => (f.choices.some((c) => c.value === f.default) ? f : { ...f, default: f.choices[0].value })) }
}
