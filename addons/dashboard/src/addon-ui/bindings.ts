// Bindings: how a declarative node reads the slot context (ticket, workspace) without any code.
//   {"$ref": "ticket.addons.estimate.points"}   -> the value at that path (null when missing)
//   "CHF ${ticket.addons.usage.cents|cents}"      -> string interpolation, optional formatter after "|"
//   "addon.byTicket.$ticket"                      -> `$ticket` is the current ticket's key (state keyed by ticket)
// Only plain property reads; no expressions, no function calls.

export const FORMATTERS: Record<string, (v: unknown) => string> = {
  cents: (v) => (Number(v) / 100).toFixed(2),
  duration: (v) => {
    const min = Math.round(Number(v) / 60000)
    return min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, '0')} min` : `${min} min`
  },
  chf: (v) => `CHF ${(Number(v) / 100).toFixed(2)}`,
  /** A real token count: "84k", "2.4 M". */
  ktok: (v) => {
    const n = Number(v)
    return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)} M` : `${Math.round(n / 1000)}k`
  },
  tokens: (v) => `${Math.round((Number(v) * 5800) / 1000)}k`,
}

export function getPath(root: unknown, path: string): unknown {
  let cur: unknown = root
  for (const raw of path.split('.')) {
    // `$ticket` stands for the key of the ticket in the slot context: {"$ref": "addon.sharesByTicket.$ticket"}.
    // The prototype guard runs on the segment AFTER that substitution, so a ticket key cannot reach the prototype.
    const part = raw === '$ticket' ? ((root as { ticket?: { key?: string } }).ticket?.key ?? '') : raw
    if (cur === null || typeof cur !== 'object' || part === '__proto__' || part === 'constructor') return undefined
    cur = (cur as Record<string, unknown>)[part]
  }
  return cur
}

function interpolate(s: string, ctx: unknown): string {
  return s.replace(/\$\{([a-zA-Z0-9_.$]+)(?:\|([a-z]+))?\}/g, (_, path: string, fmt?: string) => {
    const v = getPath(ctx, path)
    if (v === undefined || v === null) return ''
    return fmt && FORMATTERS[fmt] ? FORMATTERS[fmt](v) : String(v)
  })
}

export function resolveBindings(node: unknown, ctx: unknown): unknown {
  if (typeof node === 'string') return interpolate(node, ctx)
  if (Array.isArray(node)) return node.map((n) => resolveBindings(n, ctx))
  if (node && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    if (typeof obj.$ref === 'string' && Object.keys(obj).length === 1) return getPath(ctx, obj.$ref) ?? null
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => [k, resolveBindings(v, ctx)]))
  }
  return node
}
