import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { ReservedLead, reviewFacts } from './reviewFacts'

it('ignores folded facts and unrelated summaries', () => {
  expect(reviewFacts({ headline: 'x', summary: { title: 'Other', lines: ['Runs on this computer.'] }, notes: ['Files go to this computer.'], fields_changed: [{ field: 'server', before: null, after: 'https://publish.example.test' }] }).destination).toBe(false)
})
it('recognizes each visible fact independently, including a risk line', () => {
  expect(reviewFacts({ headline: 'x', summary: { title: 'Other', lines: [], risk: 'Anyone with the link can read the files.' } })).toMatchObject({ destination: false, publicAccess: true })
  expect(reviewFacts({ headline: 'x', summary: { title: '', lines: ['Files go to publish.example.test.'] } })).toMatchObject({ destination: true, publicAccess: false })
})
it('reserves preparing height when duplicate lines disappear', () => {
  const measure = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ height: 120 } as DOMRect)
  const view = render(<ReservedLead><p>Files go to this computer.</p></ReservedLead>)
  measure.mockReturnValue({ height: 0 } as DOMRect)
  view.rerender(<ReservedLead>{null}</ReservedLead>)
  expect(screen.getByTestId('reserved-share-lead')).toHaveStyle({ minHeight: '120px' })
  measure.mockRestore()
})
