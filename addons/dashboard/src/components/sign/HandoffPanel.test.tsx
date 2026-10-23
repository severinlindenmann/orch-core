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

const grantEvent = (type: string, extra: Record<string, unknown>) => ({ type, actor: { kind: 'person', id: 'p_sev', device: null }, auth: null, ...extra })

it('separates the command from the verification fingerprint', () => {
  const request = { ...fakeRequest(), content: { headline: 'Approve' } }
  render(<HandoffPanel request={request} />)
  expect(screen.getByText('Copy this command into Terminal')).toBeVisible()
  expect(screen.getByTestId('handoff-command')).toHaveTextContent(`orch sign ${request.code}`)
  const note = screen.getByTestId('handoff-fingerprint').parentElement!
  expect(note).toHaveTextContent(`Verification fingerprint: ${request.fingerprint}. Check that this matches the fingerprint in your terminal.`)
  expect(note).not.toHaveTextContent('same code')
  expect(screen.queryByTestId('handoff-runner-note')).toBeNull()
})

it('tells an Extend what r does, from the signed events (the stop warning is in the review notes, once)', () => {
  const events = [grantEvent('grant.issued', { grant: 'gr_new', label: 'runner' }), grantEvent('grant.revoked', { grant: 'gr_old', reason: 'extended' })]
  render(<HandoffPanel request={{ ...fakeRequest({ events }), content: { headline: 'Extend agent access' } }} />)
  const note = screen.getByTestId('handoff-runner-note')
  expect(note).not.toHaveTextContent('Running agents will stop.')
  expect(note).toHaveTextContent('choose r in Terminal to give the new access to this Mac’s agent runner')
  expect(note).toHaveTextContent('The agent runner is the part of orch on this Mac that starts agents for you.')
})

it('a plain runner grant gets the r note but not the stop warning; another grant gets neither', () => {
  const issued = (label: string) => [grantEvent('grant.issued', { grant: 'gr_new', label })]
  const { unmount } = render(<HandoffPanel request={{ ...fakeRequest({ events: issued('runner') }), content: { headline: 'g' } }} />)
  expect(screen.getByTestId('handoff-runner-note')).not.toHaveTextContent('Running agents will stop.')
  expect(screen.getByTestId('handoff-runner-note')).toHaveTextContent('choose r in Terminal')
  unmount()
  render(<HandoffPanel request={{ ...fakeRequest({ events: issued('ci') }), content: { headline: 'g' } }} />)
  expect(screen.queryByTestId('handoff-runner-note')).toBeNull()
})

it('says some values come from orch on this Mac when the host named the access taken back (material.grants)', () => {
  const base = fakeRequest()
  const request = { ...base, material: { ...base.material, grants: { gr_old: { label: 'runner', scope: 'workable', expires_at: '2026-10-25T00:37:00Z', person: 'p_x' } } }, content: { headline: 'Take back agent access' } }
  render(<HandoffPanel request={request} />)
  expect(screen.getByText('Some values come from orch on this Mac (see details)')).toBeVisible()
})

it('says why the terminal is used, once, above the command', () => {
  const request = { ...fakeRequest(), content: { headline: 'Approve' } }
  const { rerender } = render(<HandoffPanel request={request} why="terminal_only" />)
  expect(screen.getByTestId('terminal-why')).toHaveTextContent('This one is done in your terminal.')
  rerender(<HandoffPanel request={request} why="click_off" whyAction={<a href="/settings/general">Turn on One click</a>} />)
  expect(screen.getByTestId('terminal-why')).toHaveTextContent('One click is off, so sign in Terminal on this Mac.')
  expect(screen.getByRole('link', { name: 'Turn on One click' })).toBeVisible()
  // The command stays one exact, copyable field.
  expect(screen.getByRole('button', { name: 'Copy command' })).toHaveTextContent('Copy')
})
