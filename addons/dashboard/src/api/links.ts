// Ticket links to repositories (F6): the branch-name rule the page checks before it asks for a signature. The signer and
// the log apply the real rule (git's own); this mirrors it so a mistake is told in plain words first.
export const MAX_LINK_REPOS = 16

/** `feat/<ticket key in lower case>`: the suggestion offered when a repository is linked. */
export const suggestedBranch = (key: string): string => `feat/${key.toLowerCase()}`

/** What is wrong with a branch name, in plain words; `null` when it is fine (an empty name means "no branch"). */
export function branchProblem(raw: string): string | null {
  const b = raw.trim()
  if (!b) return null
  if (b.length > 200) return 'A branch name is at most 200 characters.'
  if (/[\u0000- \u007f-\u009f~^:?*[\\]/.test(b)) return 'A branch name cannot contain spaces or any of ~ ^ : ? * [ \\'
  if (b.startsWith('-')) return 'A branch name cannot start with a dash.'
  if (b === '@' || b === 'HEAD') return `“${b}” is not a branch name.`
  if (b.startsWith('/') || b.endsWith('/') || b.includes('//')) return 'A branch name cannot start or end with a slash, or have two in a row.'
  if (b.endsWith('.') || b.includes('..') || b.includes('@{')) return 'A branch name cannot end with a dot, or contain “..” or “@{”.'
  if (b.startsWith('refs/')) return 'Write the branch name only, without “refs/”.'
  if (b.split('/').some((part) => part.startsWith('.') || part.endsWith('.lock'))) return 'No part of a branch name may start with a dot or end with “.lock”.'
  return null
}
