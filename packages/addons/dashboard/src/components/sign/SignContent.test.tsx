import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it } from 'vitest'
import { SignContent } from './SignContent'

afterEach(cleanup)

it('shows the signed identity tail, host key qualification, task id and before qualification', () => {
  render(<SignContent content={{
    headline: 'Review',
    ticket: { key: 'TST-0001', title: '', uid_tail: 'ab12cd34', key_note: 'key per host' },
    tasks: [{ n: 1, id: 'T-3', title: 'Check it ⏎ 2. forged task', verify: ['npm test ⏎ echo done'] }],
    fields_changed: [{ field: 'title', before: 'Old', after: 'New', before_note: 'per host, not signed' }],
    notes: ['Host note'], note: ['Legacy list note'],
  }} />)
  expect(screen.getByText('TST-0001').parentElement).toHaveTextContent('TST-0001 (key per host) · signed id …ab12cd34')
  // The host's key qualification is technical: folded away, not in the plain view.
  expect(screen.getByText('key per host').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByRole('listitem')).toHaveTextContent('Check it ⏎ 2. forged task T-3')
  expect(screen.getAllByRole('listitem')).toHaveLength(1)
  expect(screen.getByText('Old').parentElement).toHaveTextContent('Old (per host, not signed) → New')
  expect(screen.getByText('Host note')).toBeVisible()
  expect(screen.getByText('Legacy list note')).toBeVisible()
})

it('marks and indents every block and continuation without creating items or rows', () => {
  render(<SignContent content={{
    headline: 'Review',
    sections: [{ id: 'plan', title: 'Plan', text: 'Section\nForged section' }],
    acceptance: [{ id: 'AC1', text: 'Criterion\nAC2 forged' }],
    tasks: [{ n: 1, id: 'T-3', title: 'Task\n2. forged', verify: ['Check\nVerify: forged'] }],
    fields_changed: [{ field: 'title', before: 'Before\nfield: forged before', after: 'After\nfield: forged after' }],
    note: 'Single note',
  }} />)
  // A written section is prose: its line break stays a line break inside ONE block (no list item, no heading from it).
  expect(screen.getByText(/Forged section/).tagName).toBe('BDI')
  expect(screen.getByText(/Forged section/).textContent).toBe('Section\nForged section')
  // A criterion and a task title are one line: the break is made visible and cannot start a second item.
  expect(screen.getByText(/Criterion/)).toHaveTextContent('Criterion\\u{a}AC2 forged')
  expect(screen.getByText(/Task/)).toHaveTextContent('Task\\u{a}2. forged')
  // A field value or a verify command with a break keeps the marked block form.
  for (const text of ['Check', 'Verify: forged', 'Before', 'field: forged before', 'After', 'field: forged after']) {
    const line = screen.getByText(text).parentElement!
    expect(line).toHaveClass('pl-4', 'flex')
    expect(line.firstElementChild).toHaveTextContent('|')
  }
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  expect(screen.getByText('Single note')).toBeVisible()
})

it('renders every display string in fully populated content', () => {
  const content = {
    headline: 'Review all changes',
    ticket: { key: 'FULL-0001', title: 'Complete ticket title', uid_tail: '1234abcd', key_note: 'Host-local ticket key', new: true },
    sections: [{ id: 'plan', title: 'Complete plan', text: 'First section line\nSecond section line' }],
    acceptance: [{ id: 'AC-yes', text: 'Checked criterion', checked: true }, { id: 'AC-no', text: 'Unchecked criterion', checked: false }],
    tasks: [{ n: 3, id: 'TASK-three', title: 'Complete task', verify: ['First verification', 'Second verification'] }],
    fields_changed: [
      { field: 'title', before: 'Previous title', before_note: 'Previous value from host', after: 'Updated title' },
      { field: 'removed', before: 'Removed value', after: null },
      { field: 'added', before: null, after: 'Added value' },
    ],
    notes: ['Current note one', 'Current note two'],
    note: ['Legacy note one', 'Legacy note two'],
  }
  // The dialog owns the headline; section ids are structural keys, not display values.
  render(<><h2>{content.headline}</h2><SignContent content={content} /></>)
  document.querySelectorAll('details').forEach(fold => { fold.open = true })
  for (const text of [
    content.headline,
    content.sections[0].title,
    ...content.acceptance.flatMap(a => [a.id, a.text]),
    ...content.tasks.flatMap(t => [t.id, t.title, ...t.verify]),
    ...content.fields_changed.flatMap(f => [f.before, f.before_note, f.after]).filter((v): v is string => typeof v === 'string'),
    ...content.notes, ...content.note,
  ]) expect(screen.getByText(text)).toBeVisible()
  expect(screen.getByText(/First section line/).textContent).toBe('First section line\nSecond section line')
  for (const label of ['Title:', 'Removed:', 'Added:']) expect(screen.getByText(label)).toBeVisible()
  // the ticket's key, title, signed id and the host's key note are one click away, in the ONE Technical details
  expect(screen.getAllByText('Technical details')).toHaveLength(1)
  for (const text of [content.ticket.key, content.ticket.title, content.ticket.uid_tail, content.ticket.key_note]) expect(details()).toHaveTextContent(text)
  expect(screen.getByLabelText('Checked')).toBeVisible()
  expect(screen.getByLabelText('Unchecked')).toBeVisible()
  expect(screen.getByText('TASK-three').closest('li')).toHaveAttribute('value', '3')
  expect(screen.getAllByText('(not set)')).toHaveLength(1) // the missing after-value
})

const SEV = 'p_181d953f12ab34cd56ef78901234abcd'
const MARA = 'p_aaaa11112222333344445555666677aa'
const A = 'p_aaaa1111222233334444555566667777'
const B = 'p_bbbb1111222233334444555566667777'
type M = { person: string; name: string; name_hidden?: boolean }
function withWorkspaces(workspaces: { id: string; members: M[] }[], fields: { field: string; before: string | null; after: string | null }[], ws = 'w_1') {
  const client = new QueryClient()
  client.setQueryData(['workspaces'], workspaces)
  return render(<QueryClientProvider client={client}><SignContent ws={ws} content={{ headline: 'New ticket', ticket: { key: 'RA-0001', title: '', uid_tail: 'ab12cd34', key_note: 'key per host, not proven' }, fields_changed: fields }} /></QueryClientProvider>)
}
const withMembers = (members: M[], fields: { field: string; before: string | null; after: string | null }[]) => withWorkspaces([{ id: 'w_1', members }], fields)
const row = (field: string) => screen.getByText(`${field[0].toUpperCase()}${field.slice(1)}:`).closest('dl > div')!
const details = () => screen.getByText(/Technical details/).closest('details')!

it('shows a known member as name WITH short id; the full id and the key line only under Technical details', () => {
  withMembers([{ person: SEV, name: 'Severin' }, { person: MARA, name: 'Mara' }], [{ field: 'owner', before: null, after: SEV }, { field: 'reviewer', before: MARA, after: B }])
  expect(row('owner')).toHaveTextContent('Owner: Severin · p_181d953f12ab…')
  expect(row('owner')).not.toHaveTextContent(SEV)
  expect(row('reviewer')).toHaveTextContent('Reviewer: Mara · p_aaaa11112222… → p_bbbb11112222… (unknown person)')
  expect(details()).toHaveTextContent(`Severin (p_181d953f12ab…) = ${SEV}`)
  expect(details()).toHaveTextContent('key per host, not proven')
})

// One table: every way a person id can read. `name` null = no name shown; the id is always in the row (short) and, when not cut, in full under the details.
const members: M[] = [
  { person: SEV, name: 'Severin' },
  { person: A, name: 'Sam' }, { person: B, name: 'S\u200Ba\uFF4D  ' }, // zero-width, fullwidth m, trailing spaces: the same name as Sam
  { person: 'p_cccc1111222233334444555566667777', name: 'Mal (p_dddd1111222233334444555566667777)' }, // a name that fakes an id
  { person: 'p_eeee1111222233334444555566667777', name: 'Dev  Two' }, { person: 'p_ffff1111222233334444555566667777', name: 'dev two' }, // whitespace + case variant
  { person: 'p_9999111122223333444455556666777a', name: 'hidden-name', name_hidden: true },
  { person: 'p_7777111122223333444455556666777c', name: '\uFF08p_dddd1111222233334444555566667777\uFF09' }, // fullwidth brackets: a fake id after NFKC
  { person: 'p_6666111122223333444455556666777d', name: '\u0405amuel' }, // Cyrillic Dze + Latin: mixed script
  { person: 'p_5555111122223333444455556666777e', name: 'Sev\u{1F600}' }, // emoji
  { person: 'p_4444111122223333444455556666777f', name: 'Ann\u2026' }, // ellipsis
  { person: 'p_3333111122223333444455556666777a', name: 'Anna\u20DD' }, // enclosing mark
  { person: 'p_2222111122223333444455556666777b', name: 'Zo\u0301\u0302\u0303e' }, // 3 stacked combining marks
  { person: 'p_1111111122223333444455556666777c', name: '\u0406van' }, { person: 'p_1212111122223333444455556666777c', name: 'Ivan' }, // Cyrillic I + Latin
  { person: 'p_8888111122223333444455556666777b', name: 'א\u202Eבג' }, // right-to-left name
]
const table: [string, string, string][] = [
  ['known and unique', SEV, 'Severin · p_181d953f12ab…'],
  ['confusable duplicate (zero-width, fullwidth, spaces)', A, 'p_aaaa11112222… (name shared by 2 members)'],
  ['the other one of the pair', B, 'p_bbbb11112222… (name shared by 2 members)'],
  ['a name with "(p_…)" in it', 'p_cccc1111222233334444555566667777', 'p_cccc11112222… (name not shown)'],
  ['whitespace and case variants', 'p_eeee1111222233334444555566667777', 'p_eeee11112222… (name shared by 2 members)'],
  ['fullwidth brackets and an id as a name', 'p_7777111122223333444455556666777c', 'p_777711112222… (name not shown)'],
  ['Cyrillic Dze mixed into a Latin name (next to a real Severin)', 'p_6666111122223333444455556666777d', 'p_666611112222… (name not shown)'],
  ['an emoji name', 'p_5555111122223333444455556666777e', 'p_555511112222… (name not shown)'],
  ['an ellipsis in a name', 'p_4444111122223333444455556666777f', 'p_444411112222… (name not shown)'],
  ['an enclosing mark', 'p_3333111122223333444455556666777a', 'p_333311112222… (name not shown)'],
  ['3 stacked combining marks', 'p_2222111122223333444455556666777b', 'p_222211112222… (name not shown)'],
  ['whole-Cyrillic look-alike of a Latin name counts as shared', 'p_1111111122223333444455556666777c', 'p_111111112222… (name shared by 2 members)'],
  ['hidden name', 'p_9999111122223333444455556666777a', 'p_999911112222… (name not shown)'],
  ['unknown full id', 'p_0badc0de99887766554433221100ffee', 'p_0badc0de9988… (unknown person)'],
  ['a cut id never resolves by prefix', 'p_181d953f…', 'p_181d953f… (unknown person)'],
]
it.each(table)('personLabel: %s', (_n, id, text) => {
  withMembers(members, [{ field: 'owner', before: null, after: id }])
  expect(row('owner')).toHaveTextContent(`Owner: ${text}`)
  expect(row('owner')).not.toHaveTextContent(id.length > 15 ? id : 'never')
})

it('every signed person id that is not cut appears in full under Technical details, known or not', () => {
  const ids = table.map(t => t[1]).filter(id => !id.endsWith('…'))
  withMembers(members, ids.map((id, i) => ({ field: `f${i}`, before: null, after: id })))
  for (const id of ids) expect(screen.getAllByText(id).length).toBeGreaterThan(0)
  expect(details()).toHaveTextContent('p_0badc0de99887766554433221100ffee')
})

it('a member of another workspace is an unknown person here', () => {
  withWorkspaces([{ id: 'w_1', members: [{ person: SEV, name: 'Severin' }] }, { id: 'w_2', members: [{ person: MARA, name: 'Mara' }] }], [{ field: 'owner', before: null, after: MARA }])
  expect(row('owner')).toHaveTextContent('Owner: p_aaaa11112222… (unknown person)')
  expect(row('owner')).not.toHaveTextContent('Mara')
})

it('a right-to-left name cannot reorder the id: name and id are separate isolates and the id is left-to-right', () => {
  withMembers([{ person: SEV, name: 'אבג' }], [{ field: 'owner', before: null, after: SEV }])
  // the row shows the name in its own isolate and the short id in another; the details hold the full id, left-to-right
  expect(row('owner').querySelectorAll('bdi').length).toBeGreaterThanOrEqual(2)
  expect(within(row('owner') as HTMLElement).getByTestId('person-tail')).toHaveAttribute('dir', 'ltr') // the id sits in its own left-to-right isolate
  const id = within(details()).getByText('p_181d953f12ab…')
  expect(id).toHaveAttribute('dir', 'ltr')
  expect(id.tagName).toBe('BDI')
})

it('a Cyrillic look-alike "Ѕеѵегіn" next to a Latin "Severin": both are shared, neither shows a name', () => {
  const cyr = 'p_aaaa1111222233334444555566667777', lat = 'p_bbbb1111222233334444555566667777'
  withMembers([{ person: cyr, name: '\u0405\u0435\u0475\u0435\u0433\u0456n' }, { person: lat, name: 'Severin' }], [{ field: 'a', before: cyr, after: lat }])
  expect(row('a')).toHaveTextContent('A: p_aaaa11112222… (name shared by 2 members) → p_bbbb11112222… (name shared by 2 members)')
})

it('uppercase Greek "ΝΙΚΟ" next to a Latin "NIKO" counts as shared (look-alikes map before casefolding)', () => {
  const g = 'p_aaaa1111222233334444555566667777', l = 'p_bbbb1111222233334444555566667777'
  withMembers([{ person: g, name: '\u039D\u0399\u039A\u039F' }, { person: l, name: 'NIKO' }], [{ field: 'a', before: g, after: l }])
  expect(row('a')).toHaveTextContent('A: p_aaaa11112222… (name shared by 2 members) → p_bbbb11112222… (name shared by 2 members)')
})

it('a sharp-s name next to its "ss" spelling counts as shared (same fold as host/personlabel.py)', () => {
  const a = 'p_aaaa1111222233334444555566667777', b = 'p_bbbb1111222233334444555566667777'
  withMembers([{ person: a, name: 'Stra\u00DFe' }, { person: b, name: 'Strasse' }], [{ field: 'a', before: a, after: b }])
  expect(row('a')).toHaveTextContent('A: p_aaaa11112222… (name shared by 2 members) → p_bbbb11112222… (name shared by 2 members)')
})

it('a combining mark that case-folds to a letter (U+0345 -> iota) does not survive as a letter: "Ann\u0345" is a twin of "Anni"', () => {
  const a = 'p_aaaa1111222233334444555566667777', b = 'p_bbbb1111222233334444555566667777'
  withMembers([{ person: a, name: 'Ann\u0345' }, { person: b, name: 'Anni' }], [{ field: 'a', before: a, after: b }])
  expect(row('a')).toHaveTextContent('A: p_aaaa11112222… (name shared by 2 members) → p_bbbb11112222… (name shared by 2 members)')
})

it('a name mixing covered letters with letters the table does not cover is not shown', () => {
  withMembers([{ person: SEV, name: 'Sev\u05D0' }], [{ field: 'owner', before: null, after: SEV }])
  expect(row('owner')).toHaveTextContent('Owner: p_181d953f12ab… (name not shown)')
})

it.each(['\u3164', '\u115F', '\uFFA0'])('a name with the invisible Hangul filler U+%s is not shown', (filler) => {
  withMembers([{ person: SEV, name: `Sev${filler}erin` }], [{ field: 'owner', before: null, after: SEV }])
  expect(row('owner')).toHaveTextContent('Owner: p_181d953f12ab… (name not shown)')
})

const SHARE_TEXT = [
  'Publish approval: share files outside this computer', 'Ticket: DEMO-0001', 'Prepared by: you',
  'Link: a secret link: anyone who has the link can open the files', 'Works for: 7 days from the moment the link is made',
  'The files are sent to: https://pub.example.com', 'The link opens at: https://share.example.com', 'Files (1, 1.2 KB):',
  '- report.md  (1.2 KB, sha256 0123456789abcdef)', 'Staged copy: ps_' + 'a'.repeat(32), 'Manifest: sha256:' + 'ab'.repeat(32),
].join('\n')

it('leads with the plain summary and still shows every signed line in full, labelled', () => {
  render(<SignContent content={{
    headline: 'Share files of DEMO-0001',
    summary: { title: 'Share these files', lines: ['Files: 1 (1.2 KB): report.md', 'The link works for 7 days from the moment it is made.'] },
    sections: [{ id: 'share', title: 'What will be shared', text: SHARE_TEXT }],
  }} />)
  const summary = screen.getByTestId('sign-summary')
  expect(within(summary).getByText('The link works for 7 days from the moment it is made.')).toBeVisible()
  const exact = screen.getByTestId('sign-exact')
  expect(exact).not.toHaveAttribute('open') // run-9 feedback: closed by default, still in the DOM
  expect(within(exact).getByText('What you sign')).toBeVisible()
  // every signed line, untruncated, inside the exact area
  for (const line of SHARE_TEXT.split('\n')) expect(exact.textContent).toContain(line)
  expect(summary.compareDocumentPosition(exact) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})

it('without a summary keeps the full signed text in the closed exact fold', () => {
  const odd = SHARE_TEXT.replace('7 days', '7 weeks')
  render(<SignContent content={{ headline: 'Share files', sections: [{ id: 'share', title: 'What will be shared', text: odd }] }} />)
  expect(screen.queryByTestId('sign-summary')).toBeNull()
  expect(screen.getByTestId('sign-exact')).not.toHaveAttribute('open')
  expect(screen.getByText(/Works for: 7 weeks from the moment the link is made/)).toBeVisible()
})

it('shows hidden characters in a summary line and keeps prose notes out of monospace', () => {
  render(<SignContent content={{ headline: 'x', summary: { title: 'Extend agent access', lines: ['name‮cod.exe'] }, notes: ['Running agents will stop.'] }} />)
  expect(screen.getByTestId('sign-summary')).toHaveTextContent('name\\u{202e}cod.exe')
  expect(screen.getByText('Running agents will stop.')).not.toHaveClass('font-mono')
})

it('technical details distinguish people by name and id', () => {
  const X = 'p_aaaa1111222233334444555566667777', Y = 'p_bbbb1111222233334444555566667788'
  withMembers([{ person: X, name: 'Sam' }, { person: Y, name: 'Sam Two' }], [{ field: 'owner', before: X, after: Y }])
  expect(row('owner')).toHaveTextContent('Owner: Sam · p_aaaa11112222… → Sam Two · p_bbbb11112222…')
  expect(within(row('owner') as HTMLElement).getAllByTestId('person-tail')).toHaveLength(2)
  expect(row('owner')).not.toHaveTextContent(X) // never the whole id in the row
})

it('existing and new ticket identities both stay in technical details', () => {
  const t = { key: 'DEMO-0001', title: '', uid_tail: 'WJCC73ZJ', key_note: 'key per host, not proven' }
  const { unmount } = render(<SignContent content={{ headline: 'Approve', ticket: { ...t, new: false }, notes: ['x'] }} />)
  expect(details()).toHaveTextContent('DEMO-0001 (key per host) · signed id …WJCC73ZJ')
  expect(screen.getByText('WJCC73ZJ')).not.toBeVisible()
  unmount()
  render(<SignContent content={{ headline: 'Create', ticket: { ...t, new: true }, notes: ['x'] }} />)
  expect(screen.queryByTestId('sign-ticket')).toBeNull()
  expect(screen.getByText('WJCC73ZJ').closest('details')).toBeInstanceOf(HTMLDetailsElement) // under Technical details
})

it('U3: the type is shown by a word map; an unmapped value stays exactly as signed', () => {
  render(<SignContent content={{ headline: 'x', fields_changed: [{ field: 'type', before: null, after: 'feature' }, { field: 'type', before: null, after: 'weird-kind' }] }} />)
  expect(screen.getByText('Feature')).not.toBeVisible()
  expect(screen.getByText('weird-kind')).not.toBeVisible()
})

it('names the cancelled approvals and marks incomplete host context', () => {
  const client = new QueryClient()
  client.setQueryData(['workspaces'], [{ id: 'w_1', members: [{ person: SEV, name: 'Severin' }, { person: MARA, name: 'Anna' }] }])
  render(<QueryClientProvider client={client}><SignContent ws="w_1" content={{ headline: 'Review', cancels: [{ gate: 'plan', people: [SEV, MARA], complete: false }, { gate: 'requirements', people: [SEV], complete: true }], notes: ['Existing note'] }} /></QueryClientProvider>)
  const block = screen.getByRole('region', { name: 'Signing cancels' })
  expect(block).toHaveTextContent('Plan approval (Severin, Anna) · not yet complete')
  expect(block).toHaveTextContent('Requirements approval (Severin)')
  expect(block).toHaveTextContent('(per host, not signed)')
  expect(within(block).getAllByText(/not yet complete/)).toHaveLength(1)
  expect(screen.getByText('Existing note')).toBeVisible()
})

it.each([undefined, [], null, 'bad', {}, [null], [{ gate: 'unknown', people: [SEV], complete: false }], [{ gate: 'plan', people: 'bad', complete: false }], [{ gate: 'plan', people: [SEV], complete: 'yes' }]])('ignores absent or malformed cancellation context: %j', cancels => {
  render(<SignContent content={{ headline: 'Review', cancels: cancels as never, notes: ['Existing note'] }} />)
  expect(screen.queryByText('Signing cancels:')).not.toBeInTheDocument()
  expect(screen.getByText('Existing note')).toBeVisible()
})

it('renders a host manual-acceptance annotation under the criterion', () => {
  render(<SignContent content={{ headline: 'Review result', acceptance: [{ id: 'AC2', text: 'Works', manual: 'AC2: accepted manually by Severin (no automated check)' }] }} />)
  expect(screen.getByRole('listitem')).toHaveTextContent('AC2: accepted manually by Severin (no automated check)')
})

it('shows every host summary line and the risk, never filtering or cutting them', () => {
  const lines = ['Agents may read files.', 'Access lasts one day.', 'You can stop access.', 'Existing tasks continue.', 'Device d_0123456789abcdef0123456789abcdef', 'On this computer']
  render(<SignContent content={{ headline: 'Turn on one click', summary: { title: 'Turn on one click', lines, risk: 'Anyone using this browser could approve for you.' } }} />)
  const summary = screen.getByTestId('sign-summary')
  expect(within(summary).getAllByRole('listitem')).toHaveLength(lines.length)
  for (const line of lines) expect(summary).toHaveTextContent(line)
  expect(screen.getByTestId('sign-risk')).toBeVisible()
  expect(screen.getByTestId('sign-exact')).not.toHaveAttribute('open')
})

it('keeps the ticket identity visible next to a host summary', () => {
  render(<SignContent content={{ headline: 'Approve', ticket: { key: 'DEMO-0001', uid_tail: 'abcd1234' }, summary: { title: 'Approve', lines: ['Approve the plan.'] } } as never} />)
  expect(screen.getByTestId('sign-ticket')).toHaveTextContent('DEMO-0001')
})

// run17 P01b: for Publish (built in) the ids and the capability line sit under the fold; the content itself is unchanged.
const publishOn = {
  headline: 'Turn on Publish',
  summary: { title: 'Turn on Publish', lines: ['It says it will use the internet.', 'Version 1.0.0. Runs on this computer.'], risk: 'An add-on runs its own code on this computer as you; turn it on only if you trust it.' },
  ticket: { key: 'AT-0002', title: '', uid_tail: 'ab12cd34', key_note: 'key per host' },
}

it('foldIds: the capability line and the signed id leave the main view, stay in the fold; the risk line stays', () => {
  render(<SignContent foldIds content={publishOn} />)
  const main = screen.getByTestId('sign-summary')
  expect(main).not.toHaveTextContent('use the internet')
  expect(main).toHaveTextContent('Version 1.0.0')
  expect(screen.queryByTestId('sign-ticket')).toBeNull()
  expect(screen.getByTestId('sign-risk')).toHaveTextContent('turn it on only if you trust it')
  const fold = screen.getByTestId('sign-exact')
  expect(within(fold).getByTestId('sign-folded-fact')).toHaveTextContent('It says it will use the internet.')
  expect(fold).toHaveTextContent('signed id …ab12cd34')
})

it('without foldIds (any other add-on) every line stays in the main view', () => {
  render(<SignContent content={publishOn} />)
  expect(screen.getByTestId('sign-summary')).toHaveTextContent('It says it will use the internet.')
  expect(screen.getByTestId('sign-ticket')).toHaveTextContent('signed id …ab12cd34')
})
