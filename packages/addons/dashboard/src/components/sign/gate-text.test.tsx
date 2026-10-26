import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it } from 'vitest'
import { SignContent } from './SignContent'

afterEach(cleanup)

const REQ = '- File: elefanten.json\n- Original: "mit json"\n\nQ1 answered: separate\n<!-- note -->\n# Heading from the agent'
const content = {
  headline: 'Approve the requirements of ORCHT-0004',
  ticket: { key: 'ORCHT-0004', title: '', uid_tail: 'AE7F7MWX' },
  sections: [{ id: 'context', title: 'Context', text: 'Why we do it' }, { id: 'requirements', title: 'Requirements', text: REQ }, { id: 'comment', title: 'Your comment', text: 'ok from me' }],
  acceptance: [{ id: 'AC1', text: 'It works', checked: false }],
  fields_title: 'Applies under',
  fields_changed: [{ field: 'type', before: null, after: 'feature' }],
}

it('a gate decision draws one labelled agent frame with every signed line, known titles, and the signed fields visible', () => {
  render(<SignContent content={content as never} />)
  const frame = screen.getByRole('group', { name: 'Written by the agent' })
  expect(frame).toHaveTextContent('Written by the agent: this is what you approve')
  for (const t of ['What it is about', 'What will be built', 'How we know it is done']) expect(within(frame).getByText(t)).toBeInTheDocument()
  for (const line of ['File: elefanten.json', 'Original: "mit json"', 'Q1 answered: separate', '<!-- note -->', 'Heading from the agent', 'Why we do it', 'It works'])
    expect(frame.textContent).toContain(line)
  // the person's own comment is not agent text; orch's words and the signed fields sit outside the frame
  expect(frame.textContent).not.toContain('ok from me')
  expect(screen.getByText('ok from me')).toBeInTheDocument()
  expect(screen.getByText('Applies under')).not.toBeNull()
  expect(frame.contains(screen.getByText('Applies under'))).toBe(false)
  expect(frame.contains(screen.getByText('What happens when you sign'))).toBe(false)
})

it('"Show exactly as written" shows core\'s own text of each section, in a monospace box', async () => {
  render(<SignContent content={content as never} />)
  await userEvent.click(screen.getByLabelText('Show exactly as written'))
  const frame = screen.getByRole('group', { name: 'Written by the agent' })
  expect(frame.querySelector('[data-testid="signed-text"]')).toBeNull()
  expect(within(frame).getByText((_, el) => el?.tagName === 'BDI' && el.textContent === REQ)).toBeInTheDocument()
  await userEvent.click(screen.getByLabelText('Show exactly as written'))
})

it('every section of a gate review except core\'s own lands inside the agent frame, incl. findings and an add-on id', () => {
  const c = { ...content, sections: [...content.sections, { id: 'findings', title: 'Findings', text: 'Looks fine' }, { id: 'acme.notes', title: 'Notes', text: 'Addon words' }, { id: 'zzz_unknown', title: 'Zzz', text: 'Unknown words' }] }
  render(<SignContent content={c as never} />)
  const frame = screen.getByRole('group', { name: 'Written by the agent' })
  for (const t of ['Looks fine', 'Addon words', 'Unknown words']) expect(frame.textContent).toContain(t)
  expect(frame.textContent).not.toContain('ok from me')
})

it('the frame says "this is what you approve" only when approving; a request for changes says what the decision covers', () => {
  const c = { ...content, headline: 'Ask for changes to the requirements of ORCHT-0004' }
  render(<SignContent content={c as never} />)
  const frame = screen.getByRole('group', { name: 'Written by the agent' })
  expect(frame).toHaveTextContent('Written by the agent: this is what your decision covers')
  expect(frame.textContent).not.toContain('what you approve')
})

it('a gate review is recognised by core\'s "Applies under" mark, not by the headline wording', () => {
  render(<SignContent content={{ ...content, headline: 'Something else entirely' } as never} />)
  expect(screen.getByRole('group', { name: 'Written by the agent' })).toBeInTheDocument()
  cleanup()
  render(<SignContent content={{ ...content, fields_title: undefined } as never} />)
  expect(screen.queryByRole('group', { name: 'Written by the agent' })).toBeNull()
})
