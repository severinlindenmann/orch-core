import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetSignedDrops, signedNow, withoutSigned } from './signedDrop'
import type { NeedsYouItem, TodayDocument } from './types'

const item = (ticket: string, ref: string): NeedsYouItem => ({ kind: 'approval', ticket, title: 't', text: 'x', since: '2026-10-19T10:00:00Z', ref })
const today = (...items: NeedsYouItem[]): TodayDocument => ({ now: '2026-10-19T10:00:00Z', workspace: 'w', needs_you: items, read_only_open: [] }) as unknown as TodayDocument

beforeEach(resetSignedDrops)

describe('a confirmed signature settles its item at once', () => {
  it('drops the item from the cached Today immediately and keeps the others', () => {
    const qc = new QueryClient()
    qc.setQueryData(['today', 'w'], today(item('A-1', 'requirements'), item('A-1', 'plan'), item('A-2', 'requirements')))
    signedNow(qc, 'w', 'A-1', 'requirements')
    const d = qc.getQueryData<TodayDocument>(['today', 'w'])!
    expect(d.needs_you.map((i) => `${i.ticket}/${i.ref}`)).toEqual(['A-1/plan', 'A-2/requirements'])
  })

  it('a read that still lists the signed item (the host lags) does not bring it back', () => {
    const qc = new QueryClient()
    qc.setQueryData(['today', 'w'], today(item('A-1', 'requirements')))
    signedNow(qc, 'w', 'A-1', 'requirements')
    expect(withoutSigned('w', today(item('A-1', 'requirements'))).needs_you).toEqual([])
  })

  it('once the server stops listing it, the entry ends: a reopened item shows again', () => {
    const qc = new QueryClient()
    qc.setQueryData(['today', 'w'], today(item('A-1', 'requirements')))
    signedNow(qc, 'w', 'A-1', 'requirements')
    expect(withoutSigned('w', today()).needs_you).toEqual([])
    expect(withoutSigned('w', today(item('A-1', 'requirements'))).needs_you).toHaveLength(1)
  })

  it('never outlives 30 seconds, and another workspace is untouched', () => {
    const qc = new QueryClient()
    qc.setQueryData(['today', 'w'], today(item('A-1', 'requirements')))
    signedNow(qc, 'w', 'A-1', 'requirements')
    expect(withoutSigned('other', today(item('A-1', 'requirements'))).needs_you).toHaveLength(1)
    expect(withoutSigned('w', today(item('A-1', 'requirements')), Date.now() + 31_000).needs_you).toHaveLength(1)
  })

  it('a step re-requested at a new version is not the signed one', () => {
    const qc = new QueryClient()
    const v = (hash: string): NeedsYouItem => ({ ...item('A-1', 'requirements'), hash })
    qc.setQueryData(['today', 'w'], today(v('aaaaaaaaaaaa')))
    signedNow(qc, 'w', 'A-1', 'requirements', 'sha256:aaaaaaaaaaaa1111')
    expect((qc.getQueryData<TodayDocument>(['today', 'w'])!).needs_you).toEqual([])
    expect(withoutSigned('w', today(v('bbbbbbbbbbbb'))).needs_you).toHaveLength(1)
  })

  it('refreshes today, the ticket and the board', () => {
    const qc = new QueryClient()
    qc.setQueryData(['today', 'w'], today())
    qc.setQueryData(['ticket', 'w', 'A-1'], {})
    qc.setQueryData(['board', 'w'], [])
    signedNow(qc, 'w', 'A-1', 'requirements')
    for (const k of ['today', 'ticket', 'board']) expect(qc.getQueryCache().findAll({ queryKey: [k] })[0]!.state.isInvalidated).toBe(true)
  })
})
