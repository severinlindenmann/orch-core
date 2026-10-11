import { describe, expect, it } from 'vitest'
import { REPO_NAME, remoteIdentity, remoteProblem, repoFolderProblem, resolveRepoPath } from './repos'

describe('repo rules (settings.repos)', () => {
  it('repo names follow ticket format §10', () => {
    for (const ok of ['billing-api', 'Acme.Energy_dbt', '9lives', 'a'.repeat(100)]) expect(REPO_NAME.test(ok)).toBe(true)
    for (const bad of ['', '-x', '.x', '_x', 'a/b', 'a b', 'a'.repeat(101), 'bіlling', 'ﬁle']) expect(REPO_NAME.test(bad)).toBe(false)
  })

  it.each([
    'https://git.example.test/acme/a.git',
    'https://git.example.test:8443/acme/a',
    'ssh://git.example.test/acme/a.git',
    'ssh://git.example.test:2222/acme/a.git',
    'git@git.example.test:acme/a.git',
  ])('accepts a credential-free remote: %s', (r) => expect(remoteProblem(r)).toBeNull())

  it.each([
    ['https://user:token@git.example.test/a.git', /credentials/],
    ['https://user@git.example.test/a.git', /credentials/],
    ['https://:@git.example.test/a.git', /credentials/],
    ['https://@git.example.test/a.git', /credentials/],
    ['https://us%65r:p%40ss@git.example.test/a.git', /credentials/],
    ['https://git.example.test%40evil.test/a.git', null],
    ['ssh://user:pass@git.example.test/a.git', /credentials/],
    ['ssh://git@git.example.test/a.git', /credentials/],
    ['git@-oProxyCommand=x:a.git', null],
    ['deploy@git.example.test:a.git', null],
    ['git:token@git.example.test:a.git', null],
    ['http://git.example.test/a.git', null],
    ['file:///tmp/repo', null],
    ['https:git.example.test/repo', null],
    ['https://git.example.test\\repo', null],
    ['https://git.example.test/a.git?secret=x', null],
    ['https://git.example.test/a.git#x', null],
    ['https://git.example.test/a\n.git', null],
    ['https://gіt.example.test/a.git', null],
    ['https://git.example.test/a‮.git', null],
    ['https://-git.example.test/a.git', null],
    ['https://git.example.test/', null],
    ['git@-oProxyCommand=x:y', null],
    ['ssh://-oProxyCommand=x/y', null],
    ['git@git.example.test:-u/x.git', null],
    ['git@git.example.test:acme/-u.git', null],
    ['https://git.example.test/-u/x.git', null],
    ['ssh://git.example.test/acme/--upload-pack=x', null],
  ])('refuses %s', (r, msg) => {
    const problem = remoteProblem(r)
    expect(problem).toBeTruthy()
    if (msg) expect(problem).toMatch(msg)
  })

  it.each(['..', '.', 'a..b', 'a/b', '/abs', '~/x', '-bad', '.hidden', 'a\\b', 'bіlling', 'a'.repeat(101), ''])('refuses folder %s', (f) =>
    expect(repoFolderProblem(f)).toBeTruthy(),
  )
  it.each(['billing-api', 'Acme_dbt', 'docs.site'])('accepts folder %s', (f) => expect(repoFolderProblem(f)).toBeNull())

  it('resolves a path against the workspace folder', () => {
    expect(resolveRepoPath('~/work/acme', 'billing-api')).toBe('~/work/acme/billing-api')
    expect(resolveRepoPath('~/work/acme', './billing-api/')).toBe('~/work/acme/billing-api')
    expect(resolveRepoPath('~/work/acme', '../shared/lib')).toBe('~/work/shared/lib')
    expect(resolveRepoPath('~/work/acme', '/srv/repos/x')).toBe('/srv/repos/x')
    expect(resolveRepoPath('~/work/acme', '~/work/acme/./web-portal')).toBe('~/work/acme/web-portal')
    expect(resolveRepoPath('~/work/acme', '~/work/acme/x/../web-portal/')).toBe('~/work/acme/web-portal')
    expect(resolveRepoPath('~/work/acme', '/srv/x/../repos//y/.')).toBe('/srv/repos/y')
    expect(resolveRepoPath('~/work/acme', '/../..')).toBe('/')
    expect(resolveRepoPath('/srv/ws/', 'a/./b/../c')).toBe('/srv/ws/a/c')
  })

  it('canonical remote identity unifies transports and spellings', () => {
    const id = remoteIdentity('https://git.example.test/acme/a.git')
    expect(remoteIdentity('git@GIT.example.test:acme/a')).toBe(id)
    expect(remoteIdentity('ssh://git.example.test:22/acme/a.git/')).toBe(id)
    expect(remoteIdentity('https://git.example.test:443/acme/a')).toBe(id)
    expect(remoteIdentity('https://git.example.test/acme/b.git')).not.toBe(id)
  })
})
