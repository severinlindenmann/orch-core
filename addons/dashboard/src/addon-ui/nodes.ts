// The CLOSED set of declarative node types an addon may use. Anything else is rejected at render time.
// Children of `stack` stay `unknown` here: every child is validated again when it is rendered, so one bad
// child shows the "could not be shown" box instead of taking the whole panel down.
import { z } from 'zod'
import { ARG_KEY } from '@/api/addons'

const text = z.string().max(4000)
const scalar = z.union([z.string().max(4000), z.number(), z.boolean()])
const cell = scalar.nullable()
// Ends up as a URL path segment (.../actions/<id>): never `.`/`..` or a leading dot or dash.
const actionId = z.string().regex(/^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$/)
/** Plain values sent with an action: a few, short keys. */
const argsRecord = z
  .record(z.string().regex(ARG_KEY), scalar)
  .refine((o) => Object.keys(o).length <= 16, 'at most 16 args')
const orEmpty = <T extends z.ZodType>(t: T) => z.array(t).max(500).nullish().transform((v) => v ?? [])

export const MAX_DEPTH = 6

export const stackNode = z.object({
  type: z.literal('stack'),
  direction: z.enum(['col', 'row']).default('col'),
  /** Row only: children keep their own width and sit on one line (a breadcrumb, a search box and its button) instead of sharing the width. */
  fit: z.boolean().optional(),
  children: z.array(z.unknown()).max(50),
})
export const statNode = z.object({
  type: z.literal('stat'),
  label: text,
  value: z.union([z.string().max(200), z.number()]).nullable(),
  hint: text.optional(),
  /** What a card-field stat adds to its Board column's sum, when it differs from `value` (e.g. a t-shirt size's weight). */
  sum: z.union([z.string().max(200), z.number()]).nullable().optional(),
  /** A few recent values, oldest first (e.g. cost per day): core draws them as a small sparkline where the stat is a glance line (Today). */
  trend: z.array(z.number().finite().min(-1e12).max(1e12)).max(60).optional(),
})
export const kvNode = z.object({
  type: z.literal('kv'),
  pairs: z.array(z.object({ label: text, value: cell, mono: z.boolean().optional() })).max(50),
})
const itemAction = z.object({
  label: z.string().max(40),
  action: actionId,
  /** Values of the form "$row.<key>" (table rowActions only) resolve to that row's cell value. */
  args: argsRecord.optional(),
  variant: z.enum(['primary', 'secondary', 'ghost', 'danger']).default('ghost'),
  /** The one action shown as a button when a row has several; without it the first non-danger action is. The rest go into the "More" menu. */
  primary: z.boolean().optional(),
  /** "$row.<key>": the action is offered only when that cell is truthy (a table row's, evaluated by core). "!$row.<key>" for the opposite. */
  when: z.string().regex(/^!?\$row\.[A-Za-z0-9_]{1,64}$/).optional(),
  /**
   * The action cannot run now, and this is why: drawn disabled with the reason next to it (a literal sentence, or
   * "$row.<key>" for a table row's cell; an empty cell means it can run). The host still decides.
   */
  blocked: z.string().max(120).optional(),
  /** The button's text while this row's action is running ("Stopping…"), so a slow action never looks like nothing happened. */
  pendingLabel: z.string().max(40).optional(),
  /**
   * A list item only: pressing the action opens a one-line field in the row (focused) before anything runs; the typed
   * text is sent as arg `name`. For "Close with proof"-style actions that need a sentence but no page of their own.
   */
  input: z.object({ name: z.string().regex(/^[A-Za-z][A-Za-z0-9_]{0,31}$/), label: z.string().max(60), placeholder: z.string().max(80).optional(), submitLabel: z.string().max(40).optional(), maxLength: z.number().int().min(1).max(500).optional() }).optional(),
})
export type ItemAction = z.output<typeof itemAction>

export const listNode = z.object({
  type: z.literal('list'),
  items: orEmpty(
    z.object({
      /** A stable id for the item (keeps its feedback with it when items are inserted or removed). */
      id: z.string().min(1).max(200).regex(/^[A-Za-z0-9_.:/-]+$/).optional(),
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
  columns: z.array(z.object({ key: z.string().max(64), label: text, /** How core draws the cell: `state` = a status chip with a dot; `ticket` = a link to that ticket (the value must be a ticket key). */ cell: z.enum(['state', 'ticket']).optional(), /** Numbers read right-aligned; columns of real numbers are right-aligned without this. */ align: z.enum(['left', 'right']).optional(), /** Below this table width (px) the column folds into the row's second line. Core also folds columns from the right (those without hideBelow first) when they no longer get about 100 px each; the first two columns and the actions always stay. */ hideBelow: z.number().int().min(0).max(4000).optional() })).min(1).max(12),
  rows: orEmpty(z.record(z.string(), cell)),
  // At most 4 declared. A row shows one button (the `primary` one, else the first non-danger) and the rest in a "More"
  // menu; `when` hides an action per row, so a Start / Stop pair leaves one visible. Core enforces nothing else here.
  rowActions: z.array(itemAction).max(4).optional(),
  /** Clicking the first column's text runs this action for the row (a title that opens the item); args as in rowActions. */
  rowOpen: z.object({ action: actionId, args: argsRecord.optional() }).optional(),
  /** The last row is a total: drawn bold with a rule above it. */
  totalRow: z.boolean().optional(),
  /** Shown instead of the table when there are no rows (like a list's `empty`). */
  empty: text.optional(),
  /**
   * Rows that open (an accordion, one row at a time): `nodes` holds a node per row, keyed by the value of the row's
   * `key` cell; core draws a chevron on the rows that have one and the node in a full-width row beneath. Every detail
   * node is untrusted and validated again when it is rendered, only while its row is open. Unknown keys are refused.
   */
  rowDetail: z
    .object({
      key: z.string().regex(/^[A-Za-z0-9_]{1,64}$/),
      // State that is missing or out of bounds (a `$ref` resolving to null, too many rows) drops the chevrons only:
      // the table itself still draws, as `rows` does with orEmpty.
      nodes: z
        .record(z.string().max(200), z.unknown())
        .refine((o) => Object.keys(o).length <= 500, 'at most 500 row details')
        .nullish()
        .transform((v) => v ?? {})
        .catch({}),
    })
    .strict()
    .optional(),
})
/** `toc`: core gives the headings its own ids and shows "On this page" links to them. */
export const markdownNode = z.object({ type: z.literal('markdown'), text: z.string().max(20000), toc: z.boolean().optional() })
export const codeNode = z.object({ type: z.literal('code'), language: z.string().max(32).default('text'), text: z.string().max(20000) })
export const chartNode = z.object({
  type: z.literal('chart'),
  kind: z.enum(['bar', 'line']),
  xKey: z.string().max(64).default('x'),
  series: z.array(z.object({ key: z.string().max(64), label: text })).min(1).max(5),
  points: z.array(z.record(z.string(), z.union([z.string(), z.number()]))).max(366),
  /** Heading above the chart; also its accessible name. */
  title: z.string().max(120).optional(),
  /** Bars only: `horizontal` draws one bar per row, labels on the left. */
  layout: z.enum(['vertical', 'horizontal']).optional(),
  /** Written before values on the value axis and the bar labels, e.g. "CHF". */
  unit: z.string().max(12).optional(),
  /** Bars only: print each value at the end of its bar. */
  valueLabels: z.boolean().optional(),
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
  /** A Cancel button next to submit. The form then also shows "Unsaved changes" once edited, and core asks before any other action of this addon discards them. */
  cancel: z.object({ label: z.string().max(40), action: actionId }).optional(),
  /** After the action went through, the fields are emptied and the first one is focused again (an "add another" bar). */
  reset: z.boolean().optional(),
  /** Filters: the action runs on each change (a choice at once, typed text after a short pause) and there is no submit button. */
  live: z.boolean().optional(),
  /**
   * The id of a decision on this page that this form sets the terms of (needs `cancel`): while the form holds unsaved
   * edits, core disables that decision's primary option and says why, so nobody accepts terms they have not saved.
   */
  guards: z.string().min(1).max(200).optional(),
})
export const buttonNode = z.object({
  type: z.literal('button'),
  label: z.string().max(60),
  action: actionId,
  variant: z.enum(['primary', 'secondary', 'ghost', 'danger']).default('secondary'),
  /** Plain values sent with the action (a filter chip says which filter it sets). */
  args: argsRecord.optional(),
  /** A toggle or filter chip that is on right now (drawn pressed, exposed as aria-pressed). */
  pressed: z.boolean().optional(),
  /** Why it cannot be used now: core disables the button and shows this reason with it. */
  disabled: z.string().max(160).optional(),
})
/** Characters a link address may not carry (see linkNode.href). */
const HIDDEN_IN_URL = /[\p{Cc}\p{Cf}\p{Z}\s]/u

/** An addon page inside the app; nothing else internal (no settings, no query strings). */
export const INTERNAL_LINK = /^\/addon\/[a-z0-9-]{1,40}\/[a-z0-9-]{1,40}$/

export const linkNode = z.object({
  type: z.literal('link'),
  label: z.string().max(120),
  /** An http(s) URL (opens in a new tab), or another addon's page in this app: `/addon/<name>/<page>` (core's router). */
  href: z
    .string()
    .max(2000)
    .refine((h) => /^https?:\/\//i.test(h) || INTERNAL_LINK.test(h), 'only http(s) links or /addon/<name>/<page>')
    // What the person sees must be what opens: no control, format (bidi overrides, isolates, zero-width), separator
    // or space characters, which could reorder or hide part of the address.
    .refine((h) => !HIDDEN_IN_URL.test(h), 'no control, bidi, zero-width or space characters in a link'),
  /** An http(s) link only: core shows the address itself and a Copy button beside it (an app's URL). */
  copy: z.boolean().optional(),
})

export const alertNode = z.object({ type: z.literal('alert'), tone: z.enum(['info', 'success', 'warn', 'error']), title: text, text: text.optional() })
export const progressNode = z.object({ type: z.literal('progress'), label: text, value: z.number().min(0), max: z.number().positive() })
export const frameNode = z.object({
  type: z.literal('frame'),
  title: z.string().max(120),
  html: z.string().max(200_000),
  height: z.number().int().min(80).max(1200).default(320),
})
/** One open decision of this addon (by id), rendered and signed by core in place. The id is looked up in core's list. */
export const decisionNode = z.object({ type: z.literal('decision'), id: z.string().min(1).max(200) })
/** `session` may be empty (bound to state that has no session for this ticket yet): core then draws nothing. */
export const terminalNode = z.object({ type: z.literal('terminal'), session: z.string().regex(/^[a-z0-9_-]{0,40}$/) })
/**
 * A ticket widget (format orch.widgets.v1) drawn by core: `block` is the JSON inside an `orch` fence, read by the same
 * strict parser as ticket text (fail closed). Templates run in the sandboxed frame only on the widgets addon's own
 * surfaces and only while it is active; any other addon's widget node draws core types only. `source: true` shows the
 * block beside it with a Copy button (the widgets gallery). Strict: unknown keys are refused, not stripped.
 */
export const widgetNode = z.object({ type: z.literal('widget'), block: z.string().max(64 * 1024), source: z.boolean().default(false) }).strict()
/** A jump list for a page of widget nodes: chips that scroll to the widget with that id on the same page. */
export const widgetIndexNode = z
  .object({
    type: z.literal('widget-index'),
    groups: z
      .array(z.object({ label: z.string().max(60), items: z.array(z.object({ label: z.string().max(60), widget: z.string().regex(/^[a-z][a-z0-9-]{0,39}$/) }).strict()).max(40) }).strict())
      .min(1)
      .max(6),
  })
  .strict()

/**
 * Tabs: one panel at a time, chosen by the viewer. The choice is core's own per-viewer UI state (never sent to the
 * addon). Every tab's node is untrusted and validated again when it is rendered (depth and size limits as for stack);
 * only the open tab is rendered. Unknown keys are refused.
 */
export const tabsNode = z
  .object({
    type: z.literal('tabs'),
    id: z.string().regex(/^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$/),
    tabs: z
      .array(
        z
          .object({
            id: z.string().regex(/^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}$/),
            label: z.string().min(1).max(40),
            /** A number next to the label ("Pending 3"); a missing or null count shows nothing. */
            count: z.number().int().min(0).max(1_000_000).nullish(),
            node: z.unknown(),
          })
          .strict(),
      )
      .min(1)
      .max(12)
      .refine((t) => new Set(t.map((x) => x.id)).size === t.length, 'tab ids must be unique'),
  })
  .strict()

/** A closed-by-default section ("Recently merged 3"): its node is rendered only while it is open. */
export const foldNode = z
  .object({
    type: z.literal('fold'),
    label: z.string().min(1).max(60),
    count: z.number().int().min(0).max(1_000_000).nullish(),
    node: z.unknown(),
  })
  .strict()

/** A button that opens a small panel holding one node (e.g. an "Add worktree" form). The panel renders only while open. */
export const popoverNode = z
  .object({
    type: z.literal('popover'),
    label: z.string().min(1).max(60),
    variant: z.enum(['primary', 'secondary', 'ghost']).default('secondary'),
    node: z.unknown(),
  })
  .strict()

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
  decisionNode,
  widgetNode,
  widgetIndexNode,
  tabsNode,
  foldNode,
  popoverNode,
])

export type AddonNodeData = z.output<typeof nodeSchema>
export type NodeOf<T extends AddonNodeData['type']> = Extract<AddonNodeData, { type: T }>
export const NODE_TYPES = nodeSchema.options.map((o) => o.shape.type.value)

export function parseNode(raw: unknown): { ok: true; node: AddonNodeData } | { ok: false; error: string } {
  const r = nodeSchema.safeParse(raw)
  return r.success ? { ok: true, node: r.data } : { ok: false, error: r.error.issues[0]?.message ?? 'invalid node' }
}
