// What the views draw, from `orch list --mine --json` and `orch show <id> --json`. No engine imports: the plugin's
// logic runs under `node --test` too (test/tickets.spec.ts).
import type { Move, Problem, Question, Role, Task, Ticket } from '../types'

// orch-core's role icons (orch.dashboard.data.cards.ICONS): every status is an icon and a word, never colour alone.
export const ICON: Record<Role, string> = { ok: '✓', info: '◐', you: '●', warn: '▲', err: '✕', neu: '○' }

// The only commands a refresh runs (after `orch`), each with --json. Nothing that writes, nothing human-only.
export const READS: readonly (readonly string[])[] = [['list', '--mine'], ['show']]

// Control characters (C0, DEL, C1) and bidi/zero-width marks, which could restyle the terminal or reorder text.
const HIDDEN = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩﻿]/g

// Ticket text as one safe line.
export function clean(value: unknown, max = 200): string {
  const s = String(value ?? '').replace(/[\n\t\r]+/g, ' ').replace(HIDDEN, '').trim()
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

const list = (v: unknown): any[] => (Array.isArray(v) ? v : [])
const unanswered = (q: any) => q?.answer === null || q?.answer === undefined || q?.answer === ''
const WHO = new Set(['you', 'agent', 'nobody'])

// The design-system role of orch-core's move: pink only when it is the human's.
export function roleOf(who: string, kind: string): Role {
  if (who === 'you') return 'you'
  if (kind === 'done') return 'ok'
  if (kind === 'blocked' || kind === 'stale') return 'warn'
  return kind === 'working' ? 'info' : 'neu'
}

// Shown for an orch that predates `move` in its JSON (orch-core < schema 1.4.0): no guessing.
export const OUTDATED: Move = { who: 'nobody', what: 'outdated', label: 'Update orch-core', role: 'neu', ref: null, why: null }

// Whose move it is, as orch-core decides it (`move` in `orch show --json`, its dashboard's rules).
export function moveOf(d: any): Move {
  const m = d?.move
  if (!m || typeof m !== 'object' || !WHO.has(m.who) || typeof m.kind !== 'string' || typeof m.label !== 'string') return OUTDATED
  return {
    who: m.who,
    what: clean(m.kind, 40),
    label: clean(m.label, 80),
    role: roleOf(m.who, m.kind),
    ref: m.ref === null || m.ref === undefined ? null : clean(m.ref, 40),
    why: m.why ? clean(m.why) : null,
  }
}

// A shell word the human can paste: plain when safe, else single-quoted.
function word(s: string): string {
  return /^[\w.,:@%+=-]+$/.test(s) ? s : `'${s.replace(/'/g, `'\\''`)}'`
}

export function answerCommand(id: string, qid: string, recommended: string | null): string {
  return `orch answer ${id} ${qid} ${recommended ? word(recommended) : '<answer>'}`
}

function question(d: any, id: string): Question | null {
  const q = list(d.meta?.questions).find(x => x && (x.blocking ?? true) && unanswered(x))
  if (!q) return null
  const rec = Array.isArray(q.recommended) ? q.recommended.join(',') : q.recommended
  const recommended = rec === null || rec === undefined || rec === '' ? null : clean(rec, 80)
  const qid = clean(q.id, 20)
  return {
    id: qid,
    text: clean(q.text),
    options: list(q.options).filter(o => o && o.key !== undefined).map(o => ({ key: clean(o.key, 20), label: clean(o.label, 80) })),
    recommended,
    command: answerCommand(id, qid, recommended),
  }
}

function firstPr(meta: any): Ticket['pr'] {
  const p = list(meta.prs).find(x => x && typeof x.url === 'string')
  if (!p || !/^https?:\/\//i.test(p.url)) return null
  const n = /\/(?:pull|pulls|pr|merge_requests)\/(\d+)(?:[/?#]|$)/.exec(p.url)?.[1]
  return { label: n ? `PR #${n}` : 'PR', url: p.url }
}

function taskOf(x: any): Task {
  return { id: clean(x.id, 20), state: String(x.state ?? ''), text: clean(x.text), why: x.why ? clean(x.why) : null, owner: String(x.owner ?? 'agent') }
}

// The one thing that happens next, in words: orch's own reason for the human's move, the agent's work otherwise.
function detailOf(d: any, move: Move, tasks: Task[], q: Question | null): string {
  const t = d.tasks ?? {}
  const find = (id: unknown) => tasks.find(x => x.id === id)
  if (move.what === 'outdated') return 'This orch does not say whose move it is'
  if (move.what === 'answer' && q) return q.text
  if (move.what === 'task' && find(move.ref)) return find(move.ref)!.text
  if (move.who !== 'agent') return move.why ?? ''
  for (const g of ['requirements', 'plan']) {
    if (d.meta?.gates?.[g]?.changes_requested && d.gates?.[g] !== 'approved') return `Revise the ${g}: changes requested`
  }
  const doing = find(t.doing)
  if (doing) return `${doing.id} ${doing.text}`
  if (!tasks.length) return 'No task list yet'
  if (t.can_move_to_testing === true || !list(t.open).length) return 'All tasks closed: testing next'
  const next = find(t.next)
  if (next) return `Next ${next.id} ${next.text}`
  const blocked = tasks.find(x => x.state === 'blocked')
  if (blocked) return `${blocked.id} blocked${blocked.why ? `: ${blocked.why}` : ''}`
  return ''
}

export function toTicket(d: any): Ticket {
  const meta = d.meta ?? {}
  const id = clean(d.id, 40)
  const tasks = list(d.tasks?.tasks).filter(Boolean).map(taskOf)
  const move = moveOf(d)
  const q = question(d, id)
  const verdict = meta.gates?.verify?.verdict
  return {
    id,
    title: clean(d.title),
    status: clean(d.status, 20),
    epic: meta.parent ? clean(meta.parent, 40) : null,
    move,
    detail: clean(detailOf(d, move, tasks, q)),
    gates: { requirements: String(d.gates?.requirements ?? 'pending'), plan: String(d.gates?.plan ?? 'pending') },
    verdict: verdict ? clean(verdict, 20) : null,
    tasks,
    doing: d.tasks?.doing ? clean(d.tasks.doing, 20) : null,
    closed: Number(d.tasks?.summary?.closed ?? 0),
    total: Number(d.tasks?.summary?.total ?? 0),
    question: q,
    confirm: list(meta.questions).filter(x => x && x.blocking === false && unanswered(x)).map(x => clean(x.id, 20)),
    pr: firstPr(meta),
  }
}

// The status-line word for a move: the human's move by its label, the rest by who holds it.
function short(m: Move): string {
  if (m.who === 'you') return m.label
  if (m.what === 'outdated') return 'update orch-core'
  return ['blocked', 'done', 'stale'].includes(m.what) ? m.what : 'working'
}

export function statusLine(tickets: Ticket[]): string | undefined {
  if (tickets.length === 0) return undefined
  return tickets.map(t => `${ICON[t.move.role]} ${t.id} ${short(t.move)}`).join('  ')
}

// The ticket the band leads with: the human's move first, then the agent's work, then the rest.
export function active(tickets: Ticket[]): Ticket | undefined {
  return tickets.find(t => t.move.who === 'you') ?? tickets.find(t => t.move.who === 'agent') ?? tickets[0]
}

// The exception first (doing, blocked), then what is left, then what is closed.
const ORDER: Record<string, number> = { doing: 0, blocked: 1, todo: 2, done: 3, skipped: 4 }

export function ordered(tasks: Task[]): Task[] {
  return [...tasks].sort((a, b) => (ORDER[a.state] ?? 2) - (ORDER[b.state] ?? 2))
}

// Hand-overs between the agent and the human: one toast per ticket and change.
export function changes(before: Ticket[], after: Ticket[]): string[] {
  const out: string[] = []
  for (const a of after) {
    const b = before.find(x => x.id === a.id)
    if (!b) {
      out.push(`◐ ${a.id} claimed by this session`)
      continue
    }
    const m = a.move
    if (m.who === 'you' && (b.move.who !== 'you' || b.move.label !== m.label)) {
      out.push(`● ${a.id} your move: ${m.label}`)
    } else if (b.move.who === 'you' && m.who !== 'you') {
      out.push(`✓ ${a.id} ${b.move.label}: done, the agent continues`)
    } else if (m.what === 'blocked' && b.move.label !== m.label) {
      out.push(`▲ ${a.id} ${m.label}`)
    } else if (a.verdict && a.verdict !== b.verdict) {
      out.push(`${a.verdict === 'done' ? '✓' : '▲'} ${a.id} verdict: ${a.verdict}`)
    } else if (a.status !== b.status) {
      out.push(`→ ${a.id} moved to ${a.status}`)
    }
  }
  for (const b of before) {
    if (!after.some(a => a.id === b.id)) out.push(`○ ${b.id} no longer held by this session`)
  }
  return out
}

export type Run = (argv: readonly string[]) => Promise<{ exitCode: number; stdout: string }>
export type Loaded = { tickets: Ticket[] } | { problem: Problem }

class Stop extends Error {
  problem: Problem
  constructor(problem: Problem) {
    super(problem)
    this.problem = problem
  }
}

async function orchJson(run: Run, args: string[]): Promise<any> {
  let res
  try {
    res = await run(['orch', ...args, '--json'])
  } catch (err) {
    throw new Stop(/still running|time/i.test(String((err as Error)?.message)) ? 'timeout' : 'missing')
  }
  let data
  try {
    data = JSON.parse(res.stdout)
  } catch {
    throw new Stop(res.exitCode === 0 ? 'unreadable' : 'failed')
  }
  if (res.exitCode !== 0) {
    const noWorkspace = data?.error === 'UsageError' && /config\.json/.test(String(data?.message))
    throw new Stop(noWorkspace ? 'no-workspace' : 'failed')
  }
  return data
}

// One whole read: this session's tickets, or why not. A partial read would look like a lost claim, so any failed
// read gives a problem and the caller keeps its last whole state.
export async function loadTickets(run: Run): Promise<Loaded> {
  try {
    const rows = list(await orchJson(run, ['list', '--mine']))
    const ids = rows.map(r => String(r?.id ?? '')).filter(Boolean)
    const shown = await Promise.all(ids.map(id => orchJson(run, ['show', id])))
    if (shown.some(d => !d || typeof d !== 'object' || Array.isArray(d))) return { problem: 'unreadable' }
    return { tickets: shown.map(toTicket) }
  } catch (err) {
    if (err instanceof Stop) return { problem: err.problem }
    return { problem: 'unreadable' }
  }
}

// Runs `fn` one at a time; a call during a run makes it run once more afterwards, so the newest state wins.
// A failing run is swallowed: the next call starts afresh.
export function serial(fn: () => Promise<void>): () => Promise<void> {
  let running: Promise<void> | null = null
  let again = false
  return () => {
    if (running) {
      again = true
      return running
    }
    running = (async () => {
      do {
        again = false
        await fn().catch(() => undefined)
      } while (again)
    })().finally(() => {
      running = null
    })
    return running
  }
}
