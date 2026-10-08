// Ticket widgets, format orch.widgets.v1 (plugins/orch-core/docs/widgets.md): a fenced block whose info string is exactly
// `orch`, holding one strict JSON object with exactly one of `type` (core type), `widget` (template name@version) or
// `html` (a ticket artifact plus its sha256). This file is pure: it finds the blocks, parses them strictly and applies
// the ticket-wide rules (placement, unique ids, the 40-block cap). A block that breaks any rule keeps its raw text and
// a one-line `reason`; the page draws it as code. Nothing here executes or fetches anything.

export const MAX_BLOCKS = 40
export const MAX_BLOCK_BYTES = 64 * 1024
export const MAX_DEPTH = 8
export const MAX_STRING = 20_000
export const MAX_LABEL = 200
export const MAX_ROWS = 500
export const MAX_COLUMNS = 50

export type Layer = 'type' | 'widget' | 'html'

export interface WidgetSpec {
  layer: Layer
  id?: string
  title?: string
  source?: string
  caption?: string
  /** layer 'type': the core type and its own top-level fields (unit, data, rows, ...). */
  type?: string
  fields: Record<string, unknown>
  /** layer 'widget': name@version, the pin and the block's data. */
  widget?: string
  sha256?: string
  data?: Record<string, unknown>
  /** layer 'html': the artifact name inside the ticket (and the ticket id when the path named one), height, libs. */
  html?: string
  artifact?: string
  artifactTicket?: string
  height?: number
  libs?: string[]
}

export interface Block {
  section: string
  /** 1-based line of the opening fence inside the section text. */
  line: number
  raw: string
  spec?: WidgetSpec
  /** Why this block is shown as code. Unset: the block is drawn. */
  reason?: string
  /** Position among the drawn blocks of the ticket (set by resolveTicketWidgets). */
  index?: number
}

export type Segment = { kind: 'markdown'; text: string } | { kind: 'widget'; block: Block }

// ------------------------------------------------------------------ strict JSON

/** JSON.parse with duplicate-key rejection, NaN/Infinity rejected and nesting depth limited. Throws Error(reason). */
export function strictJson(text: string, maxDepth = MAX_DEPTH): unknown {
  let i = 0
  const fail = (m: string): never => {
    throw new Error(m)
  }
  const ws = () => {
    while (i < text.length && ' \t\n\r'.includes(text[i])) i++
  }
  const value = (depth: number): unknown => {
    ws()
    const c = text[i]
    if (c === '{' || c === '[') {
      if (depth + 1 > maxDepth) fail(`nesting deeper than ${maxDepth} levels`)
      return c === '{' ? object(depth + 1) : array(depth + 1)
    }
    if (c === '"') return string()
    if (text.startsWith('true', i)) return ((i += 4), true)
    if (text.startsWith('false', i)) return ((i += 5), false)
    if (text.startsWith('null', i)) return ((i += 4), null)
    if (/^-?(NaN|Infinity)/.test(text.slice(i, i + 10))) fail('NaN and Infinity are not allowed')
    const m = /^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i))
    if (!m) return fail(i >= text.length ? 'unexpected end' : `unexpected "${text[i]}" at position ${i}`)
    i += m[0].length
    const n = Number(m[0])
    if (!Number.isFinite(n)) fail('NaN and Infinity are not allowed')
    return n
  }
  const string = (): string => {
    const m = /^"(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"/.exec(text.slice(i))
    if (!m) return fail(`bad string at position ${i}`)
    i += m[0].length
    return JSON.parse(m[0]) as string
  }
  const object = (depth: number): unknown => {
    i++
    const out: Record<string, unknown> = Object.create(null)
    ws()
    if (text[i] === '}') return (i++, out)
    for (;;) {
      ws()
      if (text[i] !== '"') fail(`expected a key at position ${i}`)
      const key = string()
      if (Object.prototype.hasOwnProperty.call(out, key)) fail(`duplicate key "${key}"`)
      ws()
      if (text[i] !== ':') fail(`expected ":" at position ${i}`)
      i++
      out[key] = value(depth)
      ws()
      if (text[i] === ',') {
        i++
        continue
      }
      if (text[i] === '}') return (i++, out)
      fail(`expected "," or "}" at position ${i}`)
    }
  }
  const array = (depth: number): unknown => {
    i++
    const out: unknown[] = []
    ws()
    if (text[i] === ']') return (i++, out)
    for (;;) {
      out.push(value(depth))
      ws()
      if (text[i] === ',') {
        i++
        continue
      }
      if (text[i] === ']') return (i++, out)
      fail(`expected "," or "]" at position ${i}`)
    }
  }
  const v = value(0)
  ws()
  if (i < text.length) fail(`unexpected "${text[i]}" at position ${i}`)
  return v
}

// ------------------------------------------------------------------ fences (CommonMark)

const OPEN = /^ {0,3}(`{3,}|~{3,})(.*)$/
const closes = (line: string, ch: string, len: number) => {
  const m = /^ {0,3}(`+|~+)\s*$/.exec(line)
  return !!m && m[1][0] === ch && m[1].length >= len
}

/**
 * Splits section text into prose and `orch` blocks. Every other fence (any other info string, any length) stays prose
 * and hides what is inside it; fences inside lists, quotes or indented code are not fences at all.
 */
export function parseSection(text: string, section: string): Segment[] {
  const lines = text.split('\n')
  const out: Segment[] = []
  let prose: string[] = []
  const flush = () => {
    if (prose.length) out.push({ kind: 'markdown', text: prose.join('\n') })
    prose = []
  }
  for (let i = 0; i < lines.length; i++) {
    const m = OPEN.exec(lines[i])
    // A backtick fence's info string cannot contain a backtick.
    if (!m || (m[1][0] === '`' && m[2].includes('`'))) {
      prose.push(lines[i])
      continue
    }
    const ch = m[1][0]
    let end = -1
    for (let j = i + 1; j < lines.length; j++)
      if (closes(lines[j], ch, m[1].length)) {
        end = j
        break
      }
    const last = end === -1 ? lines.length - 1 : end
    if (m[2].trim() !== 'orch') {
      prose.push(...lines.slice(i, last + 1))
      i = last
      continue
    }
    flush()
    const body = lines.slice(i + 1, end === -1 ? lines.length : end).join('\n')
    const block: Block = { section, line: i + 1, raw: body }
    if (end === -1) block.reason = 'fence is not closed'
    else Object.assign(block, parseBlock(body))
    out.push({ kind: 'widget', block })
    i = last
  }
  flush()
  return out
}

// ------------------------------------------------------------------ one block

const COMMON = ['id', 'title', 'source', 'caption']
const ID_RE = /^[a-z][a-z0-9-]{0,39}$/
const SHA_RE = /^[0-9a-f]{64}$/
const TEMPLATE_RE = /^[a-z][a-z0-9-]{0,39}@[1-9][0-9]{0,3}$/
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/
const AC_RE = /^AC[1-9][0-9]*$/

/** Types the format defines but this mockup does not draw. */
const OTHER_TYPES = new Set([
  'text', 'callout', 'stats', 'chips', 'links', 'health', 'gates', 'tests', 'runs', 'spark', 'series', 'bullet', 'scores', 'gantt', 'diffstat', 'diff', 'options', 'matrix', 'risk',
  'deps', 'flow', 'trail', 'deploy', 'compare', 'screens', 'video', 'gallery', 'preview', 'review', 'summary',
])

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
type Checked<T> = { ok: true; value: T } | { ok: false; reason: string }
const bad = (reason: string): { ok: false; reason: string } => ({ ok: false, reason })

function longString(v: unknown): boolean {
  if (typeof v === 'string') return v.length > MAX_STRING
  if (Array.isArray(v)) return v.some(longString)
  if (isObj(v)) return Object.entries(v).some(([k, x]) => k.length > MAX_STRING || longString(x))
  return false
}

export function parseBlock(raw: string): Pick<Block, 'spec' | 'reason'> {
  const r = check(raw)
  return r.ok ? { spec: r.value } : { reason: r.reason }
}

function check(raw: string): Checked<WidgetSpec> {
  if (new TextEncoder().encode(raw).length > MAX_BLOCK_BYTES) return bad('block is larger than 64 KiB')
  let v: unknown
  try {
    v = strictJson(raw)
  } catch (e) {
    return bad(`invalid JSON: ${(e as Error).message}`)
  }
  if (!isObj(v)) return bad('block must be a JSON object')
  const layers = (['type', 'widget', 'html'] as const).filter((k) => k in v)
  if (layers.length !== 1) return bad('block needs exactly one of type, widget or html')
  const layer = layers[0]
  if (longString(v)) return bad(`a string is longer than ${MAX_STRING} characters`)

  const own = layer === 'type' ? ['type'] : layer === 'widget' ? ['widget', 'sha256', 'data'] : ['html', 'sha256', 'data', 'libs', 'height']
  let typeKeys: string[] = []
  if (layer === 'type') {
    const t = v.type
    if (typeof t !== 'string') return bad('type must be a string')
    if (OTHER_TYPES.has(t)) return bad(`type "${t}" is not drawn in this mockup`)
    if (!CORE[t]) return bad(`unknown widget type "${t}"`)
    typeKeys = CORE[t].keys
  }
  for (const k of Object.keys(v)) if (![...COMMON, ...own, ...typeKeys].includes(k)) return bad(`unknown key "${k}"`)

  const spec: WidgetSpec = { layer, fields: {} }
  for (const [k, max] of [['title', 200], ['source', 500], ['caption', 500]] as const) {
    if (!(k in v)) continue
    if (typeof v[k] !== 'string') return bad(`${k} must be a string`)
    if ((v[k] as string).length > max) return bad(`${k} is longer than ${max} characters`)
    spec[k] = v[k] as string
  }
  if ('id' in v) {
    if (typeof v.id !== 'string' || !ID_RE.test(v.id)) return bad('id must match [a-z][a-z0-9-]{0,39}')
    spec.id = v.id
  }

  if (layer === 'type') {
    const t = v.type as string
    spec.type = t
    const r = CORE[t].check(v)
    if (r) return bad(r)
    for (const k of typeKeys) if (k in v) spec.fields[k] = v[k]
    return { ok: true, value: spec }
  }

  // Pins
  if (!('sha256' in v)) return bad(layer === 'widget' ? 'a template needs a sha256 pin' : 'an html widget needs a sha256 pin')
  if (typeof v.sha256 !== 'string' || !SHA_RE.test(v.sha256)) return bad('sha256 must be 64 hex characters')
  spec.sha256 = v.sha256
  if ('data' in v) {
    if (!isObj(v.data)) return bad('data must be an object')
    spec.data = v.data
  }
  if (layer === 'widget') {
    if (typeof v.widget !== 'string' || !TEMPLATE_RE.test(v.widget)) return bad('widget must be name@version, for example before-after@1')
    spec.widget = v.widget
    return { ok: true, value: spec }
  }
  const path = typeof v.html === 'string' ? v.html : ''
  const m = /^artifacts\/([A-Z][A-Z0-9]*-\d+)\/([^/]+)$/.exec(path) ?? /^artifact:([^/]+)$/.exec(path)
  const name = m ? m[m.length - 1] : ''
  if (!m || !NAME_RE.test(name)) return bad('html must name a ticket artifact as artifacts/<ID>/<name> or artifact:<name>')
  spec.html = path
  spec.artifact = name
  if (m.length === 3) spec.artifactTicket = m[1]
  if ('height' in v) {
    if (typeof v.height !== 'number' || !Number.isInteger(v.height) || v.height < 40 || v.height > 2000) return bad('height must be an integer from 40 to 2000')
    spec.height = v.height
  }
  if ('libs' in v) {
    if (!Array.isArray(v.libs) || v.libs.length > 8 || !v.libs.every((l) => typeof l === 'string' && /^[a-z0-9-]{1,40}$/.test(l))) return bad('libs must be a list of up to 8 library names')
    spec.libs = v.libs as string[]
  }
  return { ok: true, value: spec }
}

// ------------------------------------------------------------------ core types

const label = (s: string) => s.length <= MAX_LABEL
const cellOk = (c: unknown) => c === null || ['string', 'number', 'boolean'].includes(typeof c)

interface CoreType {
  keys: string[]
  /** A one-line reason, or undefined when the block is valid. */
  check: (v: Record<string, unknown>) => string | undefined
}

/** bars `data` as ordered [label, value] pairs (the object form keeps insertion order). */
export function barPairs(data: unknown): [string, number][] {
  if (Array.isArray(data)) return data.map((p) => [String((p as unknown[])[0]), (p as unknown[])[1] as number])
  return Object.entries(data as Record<string, number>)
}

const CORE: Record<string, CoreType> = {
  bars: {
    keys: ['unit', 'data', 'highlight'],
    check(v) {
      if (!('data' in v)) return 'bars needs data'
      let pairs: [string, unknown][]
      if (Array.isArray(v.data)) {
        if (!v.data.every((p) => Array.isArray(p) && p.length === 2 && typeof p[0] === 'string')) return 'data pairs must be [label, number]'
        pairs = v.data as [string, unknown][]
      } else if (isObj(v.data)) pairs = Object.entries(v.data)
      else return 'data must be an object of label: number or a list of [label, number]'
      if (pairs.length === 0) return 'data needs at least one bar'
      if (pairs.length > MAX_ROWS) return `more than ${MAX_ROWS} bars`
      for (const [k, x] of pairs) {
        if (!label(k)) return `a label is longer than ${MAX_LABEL} characters`
        if (typeof x !== 'number') return `value of "${k}" must be a number`
      }
      if ('unit' in v && (typeof v.unit !== 'string' || v.unit.length > 20)) return 'unit must be a short string'
      if ('highlight' in v && !pairs.some(([k]) => k === v.highlight)) return `highlight "${String(v.highlight)}" is not a label in data`
    },
  },
  table: {
    keys: ['columns', 'rows'],
    check(v) {
      if (!Array.isArray(v.columns) || !v.columns.every((c) => typeof c === 'string')) return 'columns must be a list of strings'
      if (v.columns.length === 0) return 'table needs at least one column'
      if (v.columns.length > MAX_COLUMNS) return `more than ${MAX_COLUMNS} columns`
      if (v.columns.some((c: string) => !label(c))) return `a column name is longer than ${MAX_LABEL} characters`
      if (!Array.isArray(v.rows)) return 'rows must be a list of rows'
      if (v.rows.length > MAX_ROWS) return `more than ${MAX_ROWS} rows`
      for (let r = 0; r < v.rows.length; r++) {
        const row = v.rows[r]
        if (!Array.isArray(row)) return `row ${r + 1} must be a list`
        if (row.length !== v.columns.length) return `row ${r + 1} has ${row.length} cells, expected ${v.columns.length}`
        if (!row.every(cellOk)) return `a cell in row ${r + 1} must be a string, number, boolean or null`
      }
    },
  },
  checks: {
    keys: ['rows'],
    check(v) {
      if (!Array.isArray(v.rows) || v.rows.length === 0) return 'checks needs at least one row'
      if (v.rows.length > MAX_ROWS) return `more than ${MAX_ROWS} rows`
      for (const r of v.rows) {
        if (!isObj(r)) return 'each row must be an object'
        for (const k of Object.keys(r)) if (!['ac', 'verdict', 'evidence', 'ref'].includes(k)) return `unknown key "${k}" in a row`
        if (typeof r.ac !== 'string' || !AC_RE.test(r.ac)) return 'ac must be AC<n>, for example AC1'
        if (!['met', 'not_met', 'unproven'].includes(r.verdict as string)) return 'verdict must be met, not_met or unproven'
        if (typeof r.evidence !== 'string') return 'evidence must be a string'
        if ('ref' in r && typeof r.ref !== 'string') return 'ref must be a string'
      }
    },
  },
  kv: {
    keys: ['items'],
    check(v) {
      if (!isObj(v.items)) return 'items must be an object of label: value'
      const entries = Object.entries(v.items)
      if (entries.length === 0) return 'kv needs at least one item'
      if (entries.length > MAX_COLUMNS) return `more than ${MAX_COLUMNS} items`
      for (const [k, x] of entries) {
        if (!label(k)) return `a label is longer than ${MAX_LABEL} characters`
        if (!['string', 'number', 'boolean'].includes(typeof x)) return `value of "${k}" must be a string, number or boolean`
      }
    },
  },
}

export const CORE_TYPES = ['bars', 'table', 'checks', 'kv'] as const

// ------------------------------------------------------------------ ticket-wide rules

/** Sections a widget may stand in; the others are hashed by a gate, own a grammar or are append-only. */
export const WIDGET_SECTIONS = new Set(['context', 'current_state', 'verification'])
const DEFAULT_ORDER = ['summary', 'context', 'requirements', 'out_of_scope', 'plan', 'decisions', 'verification', 'current_state']
const DEFAULT_LABELS: Record<string, string> = {
  summary: 'Summary', context: 'Context', requirements: 'Requirements', out_of_scope: 'Out of scope', plan: 'Plan', decisions: 'Decisions', verification: 'Verification', current_state: 'Current state',
}

/**
 * Every section's segments with the ticket-wide rules applied: placement, an id shared by two blocks refuses all of
 * them, and only the first 40 blocks are drawn. Drawn blocks get `index` in reading order.
 */
export function resolveTicketWidgets(
  body: Partial<Record<string, string | undefined>>,
  opts: { order?: readonly string[]; label?: (section: string) => string } = {},
): Record<string, Segment[]> {
  const order = opts.order ?? DEFAULT_ORDER
  const labelOf = opts.label ?? ((s: string) => DEFAULT_LABELS[s] ?? s)
  const out: Record<string, Segment[]> = {}
  for (const key of order) if (body[key]?.trim()) out[key] = parseSection(body[key]!, key)
  const blocks = order.flatMap((k) => (out[k] ?? []).flatMap((s) => (s.kind === 'widget' ? [s.block] : [])))
  const uses = new Map<string, number>()
  for (const b of blocks) if (b.spec?.id) uses.set(b.spec.id, (uses.get(b.spec.id) ?? 0) + 1)
  let next = 0
  blocks.forEach((b, pos) => {
    if (!b.reason && b.spec?.id && uses.get(b.spec.id)! > 1) b.reason = `id "${b.spec.id}" is used by more than one widget in this ticket`
    if (!b.reason && !WIDGET_SECTIONS.has(b.section)) b.reason = `widgets are not drawn in ${labelOf(b.section)} (a gate hashes it or it has its own grammar)`
    if (pos >= MAX_BLOCKS) b.reason = `only the first ${MAX_BLOCKS} widgets of a ticket are drawn`
    if (!b.reason) b.index = next++
  })
  return out
}
