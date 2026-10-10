// The workspace's declared repos (`settings.repos`, ticket format §2) — pure rules the host enforces and the UI reuses.
// Part of the API contract. A remote is never a secret: one that carries credentials (any userinfo) is refused.

/** A repo name as `links.repos` and `settings.repos` spell it (ticket format §10). */
export const REPO_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/

/** Printable ASCII only: no spaces, controls, bidi or zero-width marks, and no look-alike letters (IDN hosts included). */
const PLAIN = /^[\x21-\x7e]+$/
const HOST = /^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})$/
/** `git@host:path`, the scp-like SSH spelling: the literal `git` user only, a host that cannot read as an ssh option. */
const SCP = /^git@([A-Za-z0-9][A-Za-z0-9.-]{0,252}):([A-Za-z0-9_][A-Za-z0-9_./-]{0,500})$/

/** Why a remote URL is refused, or null. HTTPS, `ssh://host/path` or `git@host:path`; never userinfo, query or fragment. */
export function remoteProblem(value: unknown): string | null {
  if (typeof value !== 'string' || value.length === 0 || value.length > 1000) return 'Write a remote URL (at most 1000 characters).'
  if (!PLAIN.test(value) || value.includes('\\')) return 'Write the remote URL in plain ASCII, without spaces or hidden characters.'
  // No part may read as a command-line option (`-oProxyCommand=…`, `--upload-pack=…`), whoever runs git with it.
  if (/(^|[/:@])-/.test(value.replace(/^[a-z]+:\/\//, ''))) return 'No part of the remote may start with a dash.'
  if (value.startsWith('git@')) return SCP.test(value) ? null : 'Write an SSH remote as git@host:path (no other user, no password).'
  if (!/^(https|ssh):\/\/[^/]/.test(value)) return 'Use an HTTPS remote, git@host:path, or ssh://host/path.'
  // Any userinfo, even an empty or percent-encoded one, is refused: credentials never enter a declaration or a signature.
  const authority = value.replace(/^[a-z]+:\/\//, '').split('/')[0]
  if (authority.includes('@')) return 'Embedded credentials or userinfo are not allowed. Use a credential-free remote URL; orch never stores git credentials.'
  let u: URL
  try {
    u = new URL(value)
  } catch {
    return 'Use an HTTPS remote, git@host:path, or ssh://host/path.'
  }
  if (u.username || u.password) return 'Embedded credentials or userinfo are not allowed.'
  if (!HOST.test(u.hostname) && !/^\[[0-9a-f:]+\]$/i.test(u.hostname)) return 'Write a plain host name.'
  if (u.pathname === '/' || u.pathname === '' || u.search || u.hash || value.includes('?') || value.includes('#')) return 'The remote needs a repository path and no query or fragment.'
  return null
}

/** Canonical transport identity for duplicate checks (credentials were already refused). */
export function remoteIdentity(remote: string): string {
  const u = new URL(remote.startsWith('git@') ? `ssh://${remote.slice(4).replace(':', '/')}` : remote)
  const port = u.port && !((u.protocol === 'ssh:' && u.port === '22') || (u.protocol === 'https:' && u.port === '443')) ? `:${u.port}` : ''
  return `${u.hostname.toLowerCase()}${port}${u.pathname.replace(/\/+$/, '').replace(/\.git$/, '')}`
}

/**
 * A new working copy made from the dashboard is one folder directly in the workspace folder: the repo-name charset,
 * no `..`, no slash, not absolute, no leading dot. (`settings.repos` itself may hold any path the owner signed.)
 */
export function repoFolderProblem(folder: unknown): string | null {
  if (typeof folder !== 'string' || !REPO_NAME.test(folder) || folder.includes('..')) return 'The folder is one name in the workspace folder: letters, digits, dots, underscores or dashes (1–100), no .. or slashes.'
  return null
}

/**
 * The working copy's full path, normalised (`.`, `..`, repeated and trailing slashes): absolute (`/…`, `~/…`) as is,
 * else resolved against the workspace folder. Every duplicate check, disk lookup and clone target uses this form.
 */
export function resolveRepoPath(root: string, path: string): string {
  const full = path.startsWith('/') || path.startsWith('~/') || path === '~' ? path : `${root.replace(/\/+$/, '')}/${path}`
  const base = full.startsWith('~') ? '~' : full.startsWith('/') ? '' : '.'
  const out: string[] = []
  for (const part of full.slice(base === '.' ? 0 : base.length).split('/')) {
    if (part === '' || part === '.') continue
    if (part === '..') out.length && out[out.length - 1] !== '..' ? out.pop() : base === '' ? undefined : out.push('..')
    else out.push(part)
  }
  return base === '' ? `/${out.join('/')}` : [base === '.' ? null : base, ...out].filter((x) => x !== null).join('/') || '.'
}
