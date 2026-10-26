import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { QuestionStatus } from '@/api/types'
import { QuestionOptions, splitLabel } from './QuestionOptions'

const q = {
  id: 'Q1',
  text: 'Which way? (a) one (b) two',
  state: 'open',
  recommended: 'b',
  options: [
    { key: 'a', label: 'Keep it small: ship only the API now' },
    { key: 'b', label: 'Do both', cost: '2 days' },
  ],
} as unknown as QuestionStatus

function Pick() {
  const [v, setV] = useState<string | null>(null)
  return (
    <>
      <QuestionOptions q={q} name="n" value={v} onChange={setV} idPrefix="x" />
      <output data-testid="v">{v}</output>
    </>
  )
}

describe('question options as cards', () => {
  it('splits "Title: text" and leaves other labels whole', () => {
    expect(splitLabel('Keep it small: ship only the API now')).toEqual({ title: 'Keep it small', text: 'ship only the API now' })
    expect(splitLabel('Do both')).toEqual({ title: null, text: 'Do both' })
  })

  it('shows letter, title, text and the recommended mark; a click selects', async () => {
    render(<Pick />)
    const group = screen.getByRole('radiogroup', { name: 'Answer to Q1' })
    expect(within(group).getByText('Keep it small')).toBeInTheDocument()
    expect(within(group).getByText('ship only the API now')).toBeInTheDocument()
    expect(within(group).getAllByText('Recommended')).toHaveLength(1)
    await userEvent.click(within(group).getByText('Do both'))
    expect(screen.getByTestId('v')).toHaveTextContent('b')
    expect(within(group).getByRole('radio', { name: /^b:/ })).toBeChecked()
  })

  it('read-only lists the same cards without radios', () => {
    render(<QuestionOptions q={q} name="n" value={null} idPrefix="x" readOnly />)
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect(screen.getByRole('list', { name: 'Options for Q1' })).toHaveTextContent('Recommended')
  })

  it('shows only the label of a long-keyed option (never "option <key>"), and names Recommended and cost for a screen reader', () => {
    const long = { ...q, recommended: 'timestamp', options: [{ key: 'date', label: 'DATE' }, { key: 'timestamp', label: 'TIMESTAMP', cost: '2 days' }] } as unknown as QuestionStatus
    render(<QuestionOptions q={long} name="n" value={null} idPrefix="y" />)
    expect(screen.queryByText(/option\s+timestamp/)).toBeNull()
    expect(screen.queryByText('timestamp')).toBeNull()
    expect(screen.getByRole('radio', { name: 'TIMESTAMP, recommended, 2 days' })).toBeInTheDocument()
  })

  it('locks the radios while disabled', () => {
    render(<QuestionOptions q={q} name="n" value="a" idPrefix="z" disabled />)
    for (const r of screen.getAllByRole('radio')) expect(r).toBeDisabled()
  })
})

