import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readdirSync, readFileSync } from 'node:fs'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import type { QuestionStatus } from '@/api/types'
import items from '@/test-vectors/f1-question-items.json'
import { answerableKind, answerOf, QuestionOptions, TextAnswer, type Picked } from './QuestionOptions'

// The frozen read_item forms of core (task A), adapted the way the server does: kind and the flat keys come out of `spec`.
function fromItem(item: (typeof items)['choice_full']): QuestionStatus {
  const spec = item.spec as Record<string, unknown>
  return { ...item, ...spec, spec, state: 'open', asked_at: '', asked_by: '', answer: null } as unknown as QuestionStatus
}
const choice = fromItem(items.choice_full)
const text = fromItem(items.text_with_why_and_details as never)

function Pick({ q }: { q: QuestionStatus }) {
  const [v, setV] = useState<Picked | null>(null)
  const [o, setO] = useState('')
  return (
    <>
      <QuestionOptions q={q} name="n" value={v} onChange={setV} idPrefix="x" otherText={o} onOtherText={setO} />
      <output data-testid="v">{v ? (v.type === 'key' ? `key:${v.key}` : 'other') : ''}</output>
    </>
  )
}

describe('question options as cards (frozen fixtures)', () => {
  it('shows letter by stored order, title, text, facts chips, cost and one Recommended; a click selects', async () => {
    render(<Pick q={choice} />)
    const group = screen.getByRole('radiogroup', { name: 'Answer to Q1' })
    expect(within(group).getByText('Monthly CSV')).toBeInTheDocument()
    expect(within(group).getByText('One file a month, checked by hand')).toBeInTheDocument()
    const chips = within(group).getAllByTestId('facts')
    expect(chips[0]).toHaveTextContent('Effort ½ day')
    expect(chips[0]).toHaveTextContent('Cost CHF 400')
    expect(within(group).getAllByText('Recommended')).toHaveLength(1)
    expect(within(group).getByRole('radio', { name: 'Option A: Monthly CSV, recommended, ½ day' })).toBeInTheDocument()
    expect(within(group).getByRole('radio', { name: 'Option B: Tariff API' })).toBeInTheDocument()
    await userEvent.click(within(group).getByText('Tariff API'))
    expect(screen.getByTestId('v')).toHaveTextContent('key:api')
  })

  it('a real option with the key "other" is not the Something else card', async () => {
    render(<Pick q={choice} />)
    await userEvent.click(screen.getByText('Keep the old seeds'))
    expect(screen.getByTestId('v')).toHaveTextContent('key:other')
    expect(screen.queryByTestId('other-Q1')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: 'Something else, in your own words' }))
    expect(screen.getByTestId('v')).toHaveTextContent(/^other$/)
    await userEvent.type(screen.getByTestId('other-Q1'), 'Neither')
    expect(screen.getByText('7 / 1000')).toBeInTheDocument()
  })

  it('folds the details in a native details element, as plain text', () => {
    const { container } = render(<Pick q={choice} />)
    const d = container.querySelector('details')!
    expect(d).toHaveTextContent('If you pick CSV, we import monthly.')
    expect(d).toHaveTextContent('API means a daily pull.')
  })

  it('read-only lists the same cards without radios, including Something else', () => {
    render(<QuestionOptions q={choice} name="n" value={null} idPrefix="x" readOnly />)
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect(screen.getByRole('list', { name: 'Options for Q1' })).toHaveTextContent('Something else')
  })

  it('locks the radios while disabled', () => {
    render(<QuestionOptions q={choice} name="n" value={{ type: 'key', key: 'csv' }} idPrefix="z" disabled />)
    for (const r of screen.getAllByRole('radio')) expect(r).toBeDisabled()
  })

  it('a text question is one box with a counter and no cards', async () => {
    function T() {
      const [v, setV] = useState('')
      return <TextAnswer q={text} idPrefix="t" value={v} onChange={setV} />
    }
    render(<T />)
    expect(answerableKind(text)).toBe('text')
    await userEvent.type(screen.getByTestId('text-Q1'), 'Acme Bank')
    expect(screen.getByText('9 / 1000')).toBeInTheDocument()
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
  })

  it('answers: a key keeps the note, Other and text are the text alone, a question of unknown kind is not answerable', () => {
    expect(answerOf({ type: 'key', key: 'csv' }, ' why ')).toEqual({ option: 'csv', text: 'why' })
    expect(answerOf({ type: 'key', key: 'csv' }, '')).toEqual({ option: 'csv' })
    expect(answerOf({ type: 'other' }, 'mine')).toEqual({ text: 'mine' })
    expect(answerOf({ type: 'other' }, '  ')).toBeNull()
    expect(answerableKind({ ...choice, kind: undefined } as QuestionStatus)).toBeNull()
    expect(answerableKind({ ...choice, kind: 'form' } as unknown as QuestionStatus)).toBeNull()
  })
})

describe('no markup from the agent', () => {
  it('renders markup in a title as text and no source uses dangerouslySetInnerHTML', () => {
    const evil = { ...choice, options: [{ key: 'a', label: '<img src=x onerror=alert(1)>' }, { key: 'b', label: 'B', text: '<b>bold</b>' }] } as QuestionStatus
    const { container } = render(<QuestionOptions q={evil} name="n" value={null} idPrefix="x" />)
    expect(container.querySelector('img, b')).toBeNull()
    expect(container).toHaveTextContent('<img src=x onerror=alert(1)>')
    const bad: string[] = []
    const walk = (dir: string) => {
      for (const e of readdirSync(dir, { withFileTypes: true })) {
        const p = `${dir}/${e.name}`
        if (e.isDirectory()) walk(p)
        else if (/\.tsx?$/.test(e.name) && !/\.test\./.test(e.name) && readFileSync(p, 'utf8').includes('dangerouslySetInnerHTML')) bad.push(p)
      }
    }
    for (const d of ['src/app/pages/ticket', 'src/app/pages/today', 'src/components/sign']) walk(d)
    expect(bad).toEqual([])
  })
})
