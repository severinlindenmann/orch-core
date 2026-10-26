// Busy day: the repos the generated tickets link (`links.repos` must be in `settings.repos`, ticket format §5.11) and,
// in DEMO, a larger multi-repo harness for the Repos addon. Remotes are obviously fake; no credentials anywhere.
import type { WorkspaceRepo } from '@/api/types'
import { REPOS } from './pools'

const https = (name: string): WorkspaceRepo => ({ path: name, remote: `https://git.example.test/acme/${name}.git`, default_branch: 'main' })

const DEMO_EXTRA = ['analytics', 'notifications', 'identity', 'mobile', 'search', 'audit', 'reports', 'private-api']

export function busyRepos(prefix: string): Record<string, WorkspaceRepo> {
  const linked = Object.fromEntries(REPOS.map((r) => [r.name, https(r.name)]))
  return prefix === 'DEMO' ? { ...linked, ...Object.fromEntries(DEMO_EXTRA.map((n) => [n, https(n)])) } : linked
}
