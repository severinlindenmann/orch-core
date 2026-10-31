import { expect, it } from 'vitest'
import { destinationLine, hostOf } from '@/api/shares'

// The one plain line every publish review keeps in its main view: where the files go, by name.
it('"this computer" only when the Publish server on this computer is the known source', () => {
  expect(destinationLine('computer', null, 'http://127.0.0.1:8102')).toBe('Files go to this computer.')
  // a loopback address alone does not say so: a hub can be reached through it
  expect(destinationLine(null, null, 'http://127.0.0.1:8102')).toBe('Files go to 127.0.0.1.')
  expect(destinationLine(undefined, null, 'http://localhost:8102')).not.toContain('this computer')
})

it('a hub that supplied the address is named, also at a loopback address', () => {
  expect(destinationLine('hub', 'Anna’s hub', 'http://127.0.0.1:8102')).toBe('Files go to your hub, Anna’s hub.')
  expect(destinationLine('hub', null, 'http://127.0.0.1:8102')).toBe('Files go to your hub.')
})

it('an unknown server has no line', () => {
  expect(destinationLine(null, null, null)).toBeNull()
  expect(destinationLine('computer', null, undefined)).toBeNull()
  expect(destinationLine('hub', 'Anna’s hub', '')).toBeNull()
})

it('the host name is parsed, not cut out: user info cannot pass for the host', () => {
  expect(destinationLine(null, null, 'http://trusted.com:x@evil.net')).toBe('Files go to evil.net.')
  expect(hostOf('http://trusted.com:x@evil.net')).toBe('evil.net')
})

it('a capital scheme and host work', () => {
  expect(destinationLine(null, null, 'HTTP://Example.com')).toBe('Files go to example.com.')
})

it('an IPv6 address works', () => {
  expect(destinationLine(null, null, 'http://[::1]:8102')).toBe('Files go to [::1].')
})

it('an address that does not parse is shown whole, as given', () => {
  expect(destinationLine(null, null, 'not a url')).toBe('Files go to not a url.')
  // this one does parse (scheme "pub.example.org:", path "8102/x") but with an empty hostname, so the whole address is shown
  expect(hostOf('pub.example.org:8102/x')).toBe('pub.example.org:8102/x')
})
