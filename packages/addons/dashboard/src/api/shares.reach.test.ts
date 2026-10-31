import { describe, expect, it } from 'vitest'
import { reachNote } from './shares'

describe('reachNote: who can open the links', () => {
  it('both addresses on this computer: a local test link', () => {
    expect(reachNote('http://127.0.0.1:8103', 'http://localhost:8104', 'computer')).toMatch(/^Local test link: opens only on this computer/)
  })
  it('a hub or an unknown source: never "this computer" and never "nobody else"', () => {
    for (const from of ['hub', null] as const) {
      for (const [server, share] of [['http://127.0.0.1:8103', 'http://localhost:8104'], ['http://127.0.0.1:8103', 'https://apps.example.org'], ['https://pub.example.org', 'http://127.0.0.1:8104']] as const) {
        const note = reachNote(server, share, from) ?? ''
        expect(note).not.toMatch(/this computer|nobody else/i)
      }
    }
  })
  it('the host name is parsed: user info cannot pass for the host', () => {
    const note = reachNote('http://trusted.com:x@evil.net', 'http://127.0.0.1:8104', null)!
    expect(note).toContain('evil.net')
    expect(note).not.toContain('trusted.com')
    const other = reachNote('http://127.0.0.1:8103', 'http://trusted.com:x@evil.net', null)!
    expect(other).toContain('evil.net')
    expect(other).not.toContain('trusted.com')
  })
  it('both public: no note', () => {
    expect(reachNote('https://pub.example.org', 'https://apps.example.org')).toBeNull()
  })
  it('mixed: says which side is public, never "only on this computer" for public links', () => {
    const publicLinks = reachNote('http://127.0.0.1:8103', 'https://apps.example.org', 'computer')!
    expect(publicLinks).toContain('apps.example.org')
    expect(publicLinks).toContain('other people can reach')
    expect(publicLinks).not.toMatch(/Local test link|nobody else/)
    const publicFiles = reachNote('https://pub.example.org', 'http://127.0.0.1:8104', 'computer')!
    expect(publicFiles).toContain('pub.example.org')
    expect(publicFiles).toContain('Links open only on this computer')
  })
  it('an unknown address: no claim either way', () => {
    expect(reachNote(null, 'http://127.0.0.1:8104')).toBeNull()
  })
})
