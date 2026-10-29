// A simulated company hub offer list for the mockup and its tests (M3 H-5): two skills and two servers, one of them
// needing a separate agent user. Accepting stores the digest that was shown; a stored digest that is no longer the
// offered one is "changed". Reset between tests with `resetMockOrg`.
import type { OrgKind, OrgOffers, OrgRow, OrgServerItem, OrgServerRow, OrgShown, OrgSkillItem, OrgStatus, OrgTool } from '@/api/org'

const digest = (c: string) => 'sha256:' + c.repeat(64)

const SKILLS = [
  { name: 'style-guide', digest: digest('a'), text: 'Write in short, plain sentences.\nUse **no** jargon.\nSee [the handbook](https://intranet.example.com/handbook) for tone.' },
  { name: 'support-tone', digest: digest('b'), text: 'Answer customers kindly and say what happens next.\nNever promise a date you cannot keep.' },
]
const SERVERS = [
  { name: 'crm', digest: digest('c'), url: 'https://crm.example.com/mcp', auth: 'none' as const, secret: null, description: 'Look up customers and open deals.' },
  { name: 'billing', digest: digest('d'), url: 'https://billing.example.com/mcp', auth: 'hub_secret' as const, secret: 'billing-key', description: 'Read invoices and payment status.' },
]
const TOOLS: Record<string, { name: string; title?: string; description: string; schema?: object; pin: string; tooLong?: boolean }[]> = {
  crm: [
    { name: 'find_customer', title: 'Find a customer', description: 'Find a customer by name or email.', schema: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] }, pin: 'pin-find-1' },
    { name: 'open_deals', description: 'List the open deals of a customer.', schema: { type: 'object', properties: { customer_id: { type: 'string' } } }, pin: 'pin-deals-1' },
    { name: 'bulk_export', description: '', pin: '', tooLong: true },
  ],
  billing: [{ name: 'invoice_status', description: 'Say whether an invoice is paid.', pin: 'pin-inv-1' }],
}

/** name -> accepted digest */
let accepted = new Map<string, string>()
/** `server/tool` -> pin turned on */
let toolsOn = new Map<string, string>()
export const resetMockOrg = () => {
  accepted = new Map([['skill:support-tone', digest('0')]]) // accepted once, the offer changed since
  toolsOn = new Map()
}
resetMockOrg()

const statusOf = (kind: OrgKind, name: string, current: string, auth?: string): OrgStatus => {
  const had = accepted.get(`${kind}:${name}`)
  if (!had) return 'off'
  if (had !== current) return 'changed'
  return kind === 'mcp' && auth === 'hub_secret' ? 'needs_isolation' : 'on'
}

export function mockOrgOffers(): OrgOffers {
  const skills: OrgRow[] = SKILLS.map((s) => ({ name: s.name, digest: s.digest, status: statusOf('skill', s.name, s.digest) }))
  const mcp: OrgServerRow[] = SERVERS.map((s) => ({ name: s.name, digest: s.digest, status: statusOf('mcp', s.name, s.digest, s.auth), auth: s.auth }))
  return { available: true, hub_id: '9f3c1ab277de40c8', skills, mcp }
}

const NOTES = {
  skill: [
    "A company skill is advice from your company hub. It ranks below orch's rules and cannot widen what an agent may do.",
    'Removing a skill stops it for new runs. What a run already read stays read.',
  ],
  mcp: [
    "A company tool server is reached through this workspace's own proxy; every tool starts Off.",
    "Results from the server are untrusted text from your company's server, not instructions.",
    'For a server with no sign-in, Off only hides its tools from the agent: anyone with network access can reach it.',
    'Removing a server stops it for new calls. What a run already read stays read.',
  ],
}

export function mockOrgShown(kind: OrgKind, name: string): OrgShown | null {
  if (kind === 'skill') {
    const s = SKILLS.find((x) => x.name === name)
    if (!s) return null
    const item: OrgSkillItem = { kind: 'skill', name: s.name, digest: s.digest, text: s.text, text_hidden: false }
    return { kind, status: statusOf(kind, name, s.digest), item, notes: NOTES.skill, untrusted: true, needs_isolation: null }
  }
  const s = SERVERS.find((x) => x.name === name)
  if (!s) return null
  const tools: OrgTool[] = (TOOLS[name] ?? []).map((t) => t.tooLong
    ? { name: t.name, description: null, pin: '', status: 'too_long' as const }
    : { name: t.name, title: t.title ?? null, description: t.description, input_schema: t.schema ? JSON.stringify(t.schema, null, 2) : null, pin: t.pin, status: toolsOn.get(`${name}/${t.name}`) === t.pin ? ('on' as const) : ('off' as const) })
  const item: OrgServerItem = { kind: 'mcp', name: s.name, digest: s.digest, url: s.url, auth: s.auth, secret: s.secret, description: s.description, tools, fields_hidden: false, hidden_tools: 0 }
  const status = statusOf(kind, name, s.digest, s.auth)
  const needs = s.auth === 'hub_secret' ? 'needs a separate agent user: this server\'s secret cannot be kept from an agent here' : null
  return { kind, status, item, notes: NOTES.mcp, untrusted: true, needs_isolation: needs }
}

export interface OrgReply { status: number; json: unknown }
const bad = (status: number, code: string, message: string): OrgReply => ({ status, json: { ok: false, error: { code, message, retryable: false } } })

/** The mock's org routes as plain data in, data out (also used by the browser spec's fake host). `rest` is the path after `/api/w/<ws>/`. */
export function orgHandle(method: string, rest: string, query: URLSearchParams, body: unknown): OrgReply | null {
  if (method === 'GET' && rest === 'org') return { status: 200, json: mockOrgOffers() }
  if (method === 'GET' && rest === 'org/item') {
    const kind = query.get('kind')
    const name = query.get('name') ?? ''
    const got = kind === 'skill' || kind === 'mcp' ? mockOrgShown(kind, name) : null
    return got ? { status: 200, json: got } : bad(404, 'not_found', `No offered ${kind} called ${name}`)
  }
  if (method !== 'POST') return null
  if (rest === 'org/accept') {
    const b = body as { kind?: OrgKind; name?: string; digest?: string } | null
    const shown = b?.kind && b.name ? mockOrgShown(b.kind, b.name) : null
    if (!b || !shown) return bad(404, 'not_found', 'No such offered item')
    if (shown.item.digest !== b.digest) return bad(400, 'invalid.input', `org: changed: ${b.name} is not the version you were shown`)
    if (shown.needs_isolation) return bad(400, 'invalid.input', `org: needs_isolation: ${b.name} needs a separate agent user; it stays Off on this computer`)
    accepted.set(`${b.kind}:${b.name}`, b.digest as string)
    return { status: 200, json: { kind: b.kind, name: b.name, status: 'on' } }
  }
  if (rest === 'org/decline') {
    const b = body as { kind?: OrgKind; name?: string } | null
    if (!b?.kind || !b.name) return bad(400, 'invalid.input', 'Body must be {kind, name}')
    accepted.delete(`${b.kind}:${b.name}`)
    if (b.kind === 'mcp') for (const k of [...toolsOn.keys()]) if (k.startsWith(b.name + '/')) toolsOn.delete(k)
    return { status: 200, json: { kind: b.kind, name: b.name, status: 'off' } }
  }
  if (rest === 'org/sync') return { status: 200, json: { result: { skill: 'ok', mcp: 'ok' } } }
  if (rest === 'org/tool') {
    const b = body as { server?: string; tool?: string; on?: boolean; pin?: string } | null
    if (!b?.server || !b.tool || typeof b.on !== 'boolean') return bad(400, 'invalid.input', 'Body must be {server, tool, on}')
    const known = (TOOLS[b.server] ?? []).find((t) => t.name === b.tool)
    if (!known) return bad(404, 'not_found', 'No such tool')
    if (b.on) {
      if (known.tooLong || b.pin !== known.pin) return bad(400, 'invalid.input', `org: changed: ${b.tool} is not the version you were shown`)
      toolsOn.set(`${b.server}/${b.tool}`, b.pin)
    } else toolsOn.delete(`${b.server}/${b.tool}`)
    return { status: 200, json: { server: b.server, tool: b.tool, on: b.on } }
  }
  return null
}
