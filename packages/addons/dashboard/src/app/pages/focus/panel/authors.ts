// Who changed which file in the live worktree diff (P2-5). Authors come from the delegation list: the main session's
// files are "main", a child's files carry the child's title. A shell edit appears in no list: "author unknown".
import type { WorktreeDiffAuthors } from '@/api/focus'

const clean = (p: string) => p.replace(/^\.\//, '')
/** A transcript path may be absolute; a diff path is relative to the worktree. */
const same = (diff: string, seen: string) => {
  const a = clean(diff), b = clean(seen)
  return a === b || (b.startsWith('/') && b.endsWith(`/${a}`))
}

export function authorsOf(path: string, data?: WorktreeDiffAuthors | null): string[] {
  const out: string[] = []
  const add = (name: string) => { if (!out.includes(name)) out.push(name) }
  for (const s of data?.sessions ?? []) {
    if ((s.files ?? []).some(f => same(path, f))) add('main')
    for (const c of s.children ?? []) if ((c.files ?? []).some(f => same(path, f))) add(c.title?.trim() || 'sub-agent')
  }
  return out
}

/** The text of one file's block of a unified diff (blocks start at `diff --git`), or null when there is none. */
export function blockOf(unified: string, path: string): string | null {
  const head = `diff --git a/${path} b/${path}`
  const parts = unified.split(/^(?=diff --git )/m)
  const hit = parts.find(p => p.startsWith(`${head}\n`) || p === head)
  return hit ? hit.replace(/\n$/, '') : null
}
