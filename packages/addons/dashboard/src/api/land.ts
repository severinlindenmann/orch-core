/** Shared host/worker/UI rule. The real host supplies names from git remote configuration. */
export function normTarget(raw: string, remoteNames: readonly string[] = []): string | null {
  if (!/^[A-Za-z0-9_./-]+$/.test(raw)) return null
  const remotes = new Set(['origin', 'upstream', ...remoteNames.map((name) => name.toLowerCase())])
  let target = raw.toLowerCase()
  for (;;) {
    const before = target
    if (target.startsWith('refs/heads/')) target = target.slice(11)
    else if (target.startsWith('heads/')) target = target.slice(6)
    else if (/^(refs\/)?remotes\/[^/]+\//.test(target)) target = target.replace(/^(refs\/)?remotes\/[^/]+\//, '')
    else if (remotes.has(target.split('/')[0]) && target.includes('/')) target = target.slice(target.indexOf('/') + 1)
    if (before === target) break
  }
  // Deliberately narrower than git: ASCII branch names avoid invisible characters and lookalikes.
  if (!/^[a-z0-9_./-]{1,80}$/.test(target) || target === 'head' || target.startsWith('-')
    || /^(refs|heads|remotes|tags)(\/|$)/.test(target) || remotes.has(target.split('/')[0])
    || target.startsWith('/') || target.endsWith('/') || target.endsWith('.')
    || target.includes('//') || target.includes('..')
    || target.split('/').some((part) => part.startsWith('.') || part.endsWith('.lock'))) return null
  return target
}

export const isMain = (target: string, remoteNames: readonly string[] = []) => normTarget(target, remoteNames) === 'main'
export const defaultTarget = (targets: string[]) => targets.find((target) => target !== 'main') ?? targets[0] ?? ''
