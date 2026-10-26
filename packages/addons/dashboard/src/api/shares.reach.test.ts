import { describe, expect, it } from 'vitest'
import { reachNote } from './shares'

describe('reachNote: who can open the links', () => {
  it('both addresses on this computer: a local test link', () => {
    expect(reachNote('http://127.0.0.1:8103', 'http://localhost:8104')).toMatch(/^Local test link: opens only on this computer/)
  })
  it('both public: no note', () => {
    expect(reachNote('https://pub.example.org', 'https://apps.example.org')).toBeNull()
  })
  it('mixed: says which side is public, never "only on this computer" for public links', () => {
    const publicLinks = reachNote('http://127.0.0.1:8103', 'https://apps.example.org')!
    expect(publicLinks).toContain('apps.example.org')
    expect(publicLinks).toContain('other people can reach')
    expect(publicLinks).not.toMatch(/Local test link|nobody else/)
    const publicFiles = reachNote('https://pub.example.org', 'http://127.0.0.1:8104')!
    expect(publicFiles).toContain('pub.example.org')
    expect(publicFiles).toContain('Links open only on this computer')
  })
  it('an unknown address: no claim either way', () => {
    expect(reachNote(null, 'http://127.0.0.1:8104')).toBeNull()
  })
})
