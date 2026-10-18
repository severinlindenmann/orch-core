import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { confirmWord } from '@/api/handoff'
import { fakeRequest } from '@/test/handoffHarness'
import { HandoffPanel } from './HandoffPanel'

afterEach(cleanup)

it('keeps the confirm word and host caveat visible with technical details collapsed', () => {
  const request = { ...fakeRequest(), content: { headline: 'Approve', notes: ['Read this approval'] } }
  render(<HandoffPanel request={request} />)
  expect(screen.getByText('Technical details').closest('details')).not.toHaveAttribute('open')
  const word = confirmWord(request.material.ticket!.key, request.material.ticket!.uid)
  expect(screen.getAllByText(word).find(el => !el.closest('details'))).toBeVisible()
  expect(screen.getByTestId('handoff-command')).toBeVisible()
  expect(screen.getByTestId('handoff-fingerprint')).toBeVisible()
  expect(screen.getByText('Some values come from orch on this Mac (see details)')).toBeVisible()
  expect(screen.getByText('Gate policy (stated by the host, not recomputed)')).not.toBeVisible()
})

it('shows a workspace confirm word outside technical details', () => {
  const request = { ...fakeRequest({ material: { ticket: undefined } }), op: 'settings.set', content: { headline: 'Settings' } }
  render(<HandoffPanel request={request} workspace={{ prefix: 'DEMO', id: 'workspace12345678' }} />)
  expect(screen.getAllByText('DEMO~12345678/SETTINGS').find(el => !el.closest('details'))).toBeVisible()
})

it('shows material and the confirm word when structured content is absent', () => {
  const request = fakeRequest()
  render(<HandoffPanel request={request} />)
  expect(screen.getByText('Preview. Your terminal shows what you sign.')).toBeVisible()
  expect(screen.getByText('HOST-MATERIAL task text', { exact: false })).toBeVisible()
  expect(screen.getByText(confirmWord(request.material.ticket!.key, request.material.ticket!.uid))).toBeVisible()
  expect(screen.getByText('Gate policy (stated by the host, not recomputed)')).toBeVisible()
})

it.each(['policy', 'before', 'derived', 'none'])('only summarizes present host caveats: %s', kind => {
  const request = { ...fakeRequest({ material: {
    policy: kind === 'policy' ? { count: 2 } : {},
    before: kind === 'before' ? { title: 'Old' } : {},
    review: [{ event_id: 'event', type: 'ticket.updated', payload: {}, derived: kind === 'derived' ? { key: 'DEMO-1' } : {} }],
  } }), content: { headline: 'Review' } }
  render(<HandoffPanel request={request} />)
  const note = screen.queryByText('Some values come from orch on this Mac (see details)')
  if (kind === 'none') expect(note).toBeNull()
  else expect(note).toBeVisible()
})
