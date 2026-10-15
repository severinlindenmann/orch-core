// A ticket's branch as core sees it (D53, owner decision 2026-10-10): the commits on it, its head and its diff against
// the base. The mock has no git: commits come from the log (`task.done` receipts with a commit, `branch.pushed` by an
// agent), and the diff is generated from them, deterministically, so the diffstat in the ticket and the Changes view
// always agree. A ticket with no recorded commit has one: the work the agent did before the log says so.
import type { OrchEvent, TicketBranch, TicketChanges, TicketDefinition } from '@/api/types'
import { actorLabel, fnvHex } from './derive'

/** The landing target every branch is compared with in the mock. */
export const BASE_BRANCH = 'develop'

export type Commit = TicketBranch['commits'][number]

/** The commit a log event adds to the branch, or null. */
export function commitOf(e: OrchEvent): Commit | null {
  if (e.type === 'task.done') {
    const sha = (e.receipt as { commit?: unknown } | undefined)?.commit
    return typeof sha === 'string' && sha ? { sha, at: e.at, by: actorLabel(e.actor), task: e.task as string } : null
  }
  if (e.type === 'branch.pushed' && typeof e.sha === 'string') return { sha: e.sha, at: e.at, by: actorLabel(e.actor) }
  return null
}

/** The commit a ticket's branch starts with when its log names none (stable per ticket). */
export const firstCommit = (def: Pick<TicketDefinition, 'uid'>, at: string, by: string): Commit => ({ sha: fnvHex(`${def.uid}|first`, 7), at, by })

export const branchName = (def: Pick<TicketDefinition, 'key' | 'links'>) => Object.values(def.links.branches)[0] ?? `feat/${def.key}`

const AREAS = [
  ['models/staging', 'sql'],
  ['models/marts', 'sql'],
  ['tests', 'py'],
  ['macros', 'sql'],
  ['seeds', 'csv'],
] as const

/** One file a commit changed, with a unified diff of a few lines. */
function fileOf(def: Pick<TicketDefinition, 'key' | 'title'>, c: Commit, i: number): TicketChanges['files'][number] {
  const h = parseInt(fnvHex(`${c.sha}|${i}`, 6), 16)
  const [dir, ext] = AREAS[h % AREAS.length]
  const word = def.title.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').split('_').filter((w) => w.length > 3)[i % 3] ?? 'change'
  const path = `${dir}/${c.task ? `${c.task.toLowerCase()}_` : ''}${word}.${ext}`
  const adds = 2 + (h % 9)
  const dels = (h >> 4) % 4
  const comment = ext === 'py' ? '#' : ext === 'csv' ? '' : '--'
  const lines = [
    `@@ -${10 + (h % 40)},${dels + 2} +${10 + (h % 40)},${adds + 2} @@`,
    ` ${comment} ${def.key}${c.task ? ` ${c.task}` : ''}`.trimEnd(),
    ...Array.from({ length: dels }, (_, k) => `-${ext === 'csv' ? `old_${k},${k}` : `  old_${word}_${k}`}`),
    ...Array.from({ length: adds }, (_, k) => `+${ext === 'csv' ? `${word}_${k},${k + 1}` : ext === 'py' ? `    assert ${word}_${k}()` : `  ${word}_${k} as ${word}_${k}_v2,`}`),
    ` ${ext === 'py' ? '' : 'from source'}`.trimEnd(),
  ]
  return { path, additions: adds, deletions: dels, lines: lines.join('\n') }
}

/** The branch's diff against its base: one or two files per commit (a later commit to the same path adds to it). */
export function changesOf(def: Pick<TicketDefinition, 'key' | 'title' | 'links'>, commits: Commit[]): TicketChanges {
  const byPath = new Map<string, TicketChanges['files'][number]>()
  for (const c of commits) {
    const n = 1 + (parseInt(fnvHex(c.sha, 2), 16) % 2)
    for (let i = 0; i < n; i++) {
      const f = fileOf(def, c, i)
      const prev = byPath.get(f.path)
      byPath.set(f.path, prev ? { ...prev, additions: prev.additions + f.additions, deletions: prev.deletions + f.deletions, lines: `${prev.lines}\n${f.lines}` } : f)
    }
  }
  const files = [...byPath.values()]
  return {
    ticket: def.key,
    branch: branchName(def),
    base: BASE_BRANCH,
    head: commits[commits.length - 1]?.sha ?? '',
    additions: files.reduce((a, f) => a + f.additions, 0),
    deletions: files.reduce((a, f) => a + f.deletions, 0),
    files,
  }
}

/** The ticket's branch as the ticket document carries it. */
export function branchOf(def: Pick<TicketDefinition, 'key' | 'title' | 'links'>, commits: Commit[]): TicketBranch {
  const ch = changesOf(def, commits)
  return { name: ch.branch, base: ch.base, head: ch.head, commits, files: ch.files.length, additions: ch.additions, deletions: ch.deletions }
}

/** A new commit an agent pushes (the demo of "new commits after the verdict"): stable per ticket and count. */
export const nextCommitSha = (def: Pick<TicketDefinition, 'uid'>, n: number) => fnvHex(`${def.uid}|push|${n}`, 7)
