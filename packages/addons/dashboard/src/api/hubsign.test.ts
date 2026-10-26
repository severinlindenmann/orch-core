import { expect, it } from 'vitest'
import { groupHash, hubOnly, hubRequestOf, safeHubUrl } from './hubsign'

const H = 'sha256:' + '3d3f5e93357c' + 'a'.repeat(52)
const W = 'a'.repeat(32)
const T = '01J9ZP0000000000000000000S'

it('formats a hub hash as 12 lower-case hex digits in groups of four, and refuses anything else', () => {
  expect(groupHash(H)).toBe('3d3f 5e93 357c')
  expect(groupHash('3d3f 5e93 357c')).toBe('3d3f 5e93 357c')
  for (const bad of ['sha256:' + 'A'.repeat(64), 'sha256:abc', H + 'f', H.slice(7), '', null, undefined, 12, 'sha256:' + '3d3f5e93357c' + 'a'.repeat(51)])
    expect(groupHash(bad)).toBeNull()
})

it('shows a link only when it is exactly the host grammar', () => {
  const good = [`https://hub.example.test:4443/#/w/${W}/t/${T}/g/plan`, `https://hub.example.test/#/w/${W}/t/${T}`, `https://[::1]:8443/#/w/${W}/t/${T}`]
  for (const u of good) expect(safeHubUrl(u)).toBe(u)
  const bad = [
    `http://hub.example.test/#/w/${W}/t/${T}`,
    `https://evil.example/x`,
    `https://u@hub.example.test/#/w/${W}/t/${T}`,
    `https://hub.example.test/#/w/${W}/t/${T}/g/../x`,
    `https://hub.example.test/#/w/${W}/t/${T}?x=1`,
    `javascript:alert(1)`,
    `https://hub.example.test/#/w/${W}/t/${T}\nfoo`,
    null,
  ]
  for (const u of bad) expect(safeHubUrl(u)).toBeNull()
})

it('full and unknown take no local key; other modes and an unknown host do', () => {
  expect(hubOnly({ signing: 'full' })).toBe(true)
  expect(hubOnly({ signing: 'unknown' })).toBe(true)
  for (const s of ['standalone', 'selective', null, undefined] as const) expect(hubOnly({ signing: s })).toBe(false)
  expect(hubOnly(undefined)).toBe(false)
})

it('names only the actions the hub can build', () => {
  expect(hubRequestOf({ action: 'approve', key: 'DEMO-1', gate: 'plan' })).toEqual({ kind: 'gate', ticket: 'DEMO-1', gate: 'plan' })
  expect(hubRequestOf({ action: 'verdict', key: 'DEMO-1', outcome: 'pass' })).toEqual({ kind: 'verdict', ticket: 'DEMO-1', gate: 'verify' })
  expect(hubRequestOf({ action: 'answer', key: 'DEMO-1', question: 'Q1' })).toEqual({ kind: 'question', ticket: 'DEMO-1', question: 'Q1' })
  expect(hubRequestOf({ action: 'comment', key: 'DEMO-1', message: 'x' } as never)).toBeNull()
  expect(hubRequestOf(null)).toBeNull()
})
