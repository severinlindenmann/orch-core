// The CLOSED set of declarative node types an addon may use. Anything else is rejected at render time.
// Children of `stack` stay `unknown` here: every child is validated again when it is rendered, so one bad
// child shows the "could not be shown" box instead of taking the whole panel down.
import { z } from 'zod'

const text = z.string().max(4000)
const scalar = z.union([z.string().max(4000), z.number(), z.boolean()])
const cell = scalar.nullable()
// Ends up as a URL path segment (.../actions/<id>): never `.`/`..` or a leading dot or dash.
const actionId = z.string().regex(/^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$/)
const orEmpty = <T extends z.ZodType>(t: T) => z.array(t).max(500).nullish().transform((v) => v ?? [])

export const MAX_DEPTH = 6

export const stackNode = z.object({
  type: z.literal('stack'),
  direction: z.enum(['col', 'row']).default('col'),
  children: z.array(z.unknown()).max(50),
})
export const statNode = z.object({
  type: z.literal('stat'),
  label: text,
  value: z.union([z.string().max(200), z.number()]).nullable(),
  hint: text.optional(),
  /** What a card-field stat adds to its Board column's sum, when it differs from `value` (e.g. a t-shirt size's weight). */
  sum: z.union([z.string().max(200), z.number()]).nullable().optional(),
})
export const kvNode = z.object({
  type: z.literal('kv'),
  pairs: z.array(z.object({ label: text, value: cell, mono: z.boolean().optional() })).max(50),
})
const itemAction = z.object({
  label: z.string().max(40),
  action: actionId,
  /** Values of the form "$row.<key>" (table rowActions only) resolve to that row's cell value. */
  args: z.record(z.string(), scalar).optional(),
  variant: z.enum(['primary', 'secondary', 'ghost', 'danger']).default('ghost'),
})
export type ItemAction = z.output<typeof itemAction>

export const listNode = z.object({
  type: z.literal('list'),
  items: orEmpty(
    z.object({
      title: text,
      subtitle: text.optional(),
      badge: text.optional(),
      actions: z.array(itemAction).max(3).optional(),
      status: z.enum(['ok', 'warn', 'error', 'idle', 'running']).optional(),
    }),
  ),
  empty: text.optional(),
})
export const tableNode = z.object({
  type: z.literal('table'),
  columns: z.array(z.object({ key: z.string().max(64), label: text })).min(1).max(12),
  rows: orEmpty(z.record(z.string(), cell)),
  rowActions: z.array(itemAction).max(3).optional(),
  /** Shown instead of the table when there are no rows (like a list's `empty`). */
  empty: text.optional(),
})
export const markdownNode = z.object({ type: z.literal('markdown'), text: z.string().max(20000) })
export const codeNode = z.object({ type: z.literal('code'), language: z.string().max(32).default('text'), text: z.string().max(20000) })
export const chartNode = z.object({
  type: z.literal('chart'),
  kind: z.enum(['bar', 'line']),
  xKey: z.string().max(64).default('x'),
  series: z.array(z.object({ key: z.string().max(64), label: text })).min(1).max(5),
  points: z.array(z.record(z.string(), z.union([z.string(), z.number()]))).max(366),
})
export const formNode = z.object({
  type: z.literal('form'),
  schema: z
    .record(z.string(), z.unknown())
    .refine((s) => !/"\$(ref|id)"\s*:\s*"https?:/i.test(JSON.stringify(s)), 'remote schema references are not allowed'),
  uiSchema: z.record(z.string(), z.unknown()).optional(),
  formData: z.record(z.string(), z.unknown()).nullish(),
  action: actionId,
  submitLabel: z.string().max(60).optional(),
})
export const buttonNode = z.object({
  type: z.literal('button'),
  label: z.string().max(60),
  action: actionId,
  variant: z.enum(['primary', 'secondary', 'ghost', 'danger']).default('secondary'),
})
export const linkNode = z.object({
  type: z.literal('link'),
  label: z.string().max(120),
  href: z.string().max(2000).refine((h) => /^https?:\/\//i.test(h), 'only http(s) links'),
})

export const alertNode = z.object({ type: z.literal('alert'), tone: z.enum(['info', 'success', 'warn', 'error']), title: text, text: text.optional() })
export const progressNode = z.object({ type: z.literal('progress'), label: text, value: z.number().min(0), max: z.number().positive() })
export const frameNode = z.object({
  type: z.literal('frame'),
  title: z.string().max(120),
  html: z.string().max(200_000),
  height: z.number().int().min(80).max(1200).default(320),
})
export const terminalNode = z.object({ type: z.literal('terminal'), session: z.string().regex(/^[a-z0-9_-]{1,40}$/) })

export const nodeSchema = z.discriminatedUnion('type', [
  stackNode,
  statNode,
  kvNode,
  listNode,
  tableNode,
  markdownNode,
  codeNode,
  chartNode,
  formNode,
  buttonNode,
  linkNode,
  alertNode,
  progressNode,
  frameNode,
  terminalNode,
])

export type AddonNodeData = z.output<typeof nodeSchema>
export type NodeOf<T extends AddonNodeData['type']> = Extract<AddonNodeData, { type: T }>
export const NODE_TYPES = nodeSchema.options.map((o) => o.shape.type.value)

export function parseNode(raw: unknown): { ok: true; node: AddonNodeData } | { ok: false; error: string } {
  const r = nodeSchema.safeParse(raw)
  return r.success ? { ok: true, node: r.data } : { ok: false, error: r.error.issues[0]?.message ?? 'invalid node' }
}
