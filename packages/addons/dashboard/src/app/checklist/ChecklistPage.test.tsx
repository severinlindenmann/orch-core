// The tester's checklist page: every step of run 4, status/notes/screenshots with autosave, the filter and the report.
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import * as testrun from '@/api/testrun'
import { ALL_STEPS, RUN2_SECTIONS, RUN_ID } from './run2'
import { buildReport } from './report'
import { carryFor, stepHash } from './carry'

vi.mock('@/api/testrun', async (orig) => {
  const real = await orig<typeof import('@/api/testrun')>()
  return { ...real, loadRun: vi.fn(), loadPrevious: vi.fn(), saveStep: vi.fn(), addImage: vi.fn(), removeImage: vi.fn(), imageUrl: vi.fn(), downloadScreenshots: vi.fn() }
})
const m = vi.mocked(testrun)
const T = { timeout: 8000 }
const PATH = '/Users/x/.config/orch/testrun/run4'

beforeEach(() => {
  vi.clearAllMocks()
  m.loadRun.mockResolvedValue({ path: PATH, images_path: `${PATH}/images`, notes: {} })
  m.loadPrevious.mockResolvedValue([])
  m.saveStep.mockResolvedValue(undefined)
  m.imageUrl.mockResolvedValue('data:image/png;base64,AAAA')
})

describe('run 4 data', () => {
  it('has unique ids the server accepts, and every section of the source file', () => {
    const ids = ALL_STEPS.map((s) => s.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const id of ids) expect(id).toMatch(/^[A-Z][0-9]{1,2}(\.[0-9]{1,2})?$/)
    expect(RUN2_SECTIONS.map((s) => s.id)).toEqual(['A', 'B', 'C', 'D', 'E', 'F', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'R', 'U', 'V', 'W', 'X', 'Y', 'Z', 'S', 'T'])
    for (const id of ['A1', 'A2', 'B1.1', 'B1.7', 'B2.8', 'B3.9', 'C1', 'C8', 'D6', 'E6', 'F8']) expect(ids).toContain(id)
  })
  it('holds no secret material; A1 names no fake install command and no placeholder', () => {
    const all = JSON.stringify(RUN2_SECTIONS)
    expect(all).not.toMatch(/BEGIN [A-Z ]*PRIVATE KEY|ORCH_GRANT=[A-Za-z0-9_-]{16,}|\.p8/)
    expect(all).not.toContain('PASTE-THE-COMMIT')
    const a1 = ALL_STEPS.find((s) => s.id === 'A1')!
    expect(a1.commands).toBeUndefined()
    expect(a1.text).toContain('Use the install line the lead sent you (it names the exact version).')
    expect(a1.text).toContain('orch host enable')
    expect(a1.text).toContain('orch dashboard restart')
    expect(JSON.stringify(ALL_STEPS)).not.toContain('model the model')
    expect(JSON.stringify(ALL_STEPS.find((s) => s.id === 'H1'))).toContain('File Explorer')
  })
})

describe('the checklist page', () => {
  it('shows the steps, the known gaps and where the notes are saved, without a workspace session call', async () => {
    renderApp('/checklist')
    expect(await screen.findByRole('heading', { name: /Test checklist/ }, T)).toBeInTheDocument()
    expect(await screen.findByText(PATH, {}, T)).toBeInTheDocument()
    expect(screen.getByText(/Your notes are saved in/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Known gaps/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Install with the line from the lead' })).toBeInTheDocument()
    expect(screen.getAllByRole('radiogroup')).toHaveLength(ALL_STEPS.length)
    // standalone: no dashboard sidebar around it
    expect(screen.queryByRole('link', { name: 'Settings' })).toBeNull()
  })

  it('copies a command with its copy button', async () => {
    const user = userEvent.setup()
    renderApp('/checklist')
    const step = (await screen.findByRole('heading', { name: 'Turn the host on' }, T)).closest('section')!
    await user.click(within(step).getByRole('button', { name: /^Copy: orch host enable/ }))
    expect(await navigator.clipboard.readText()).toBe('orch host enable')
  })

  it('saves a status at once and a note after a pause (autosave)', async () => {
    renderApp('/checklist')
    const group = await screen.findByRole('radiogroup', { name: 'Result of C1' }, T)
    await userEvent.click(within(group).getByRole('radio', { name: 'Failed, blocking' }))
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledWith('run4', 'C1', { status: 'fail_blocking' }))
    await userEvent.type(screen.getByLabelText('Notes for C1'), 'blank page')
    await waitFor(() => expect(m.saveStep).toHaveBeenLastCalledWith('run4', 'C1', { note: 'blank page' }), T)
    expect((await screen.findAllByText('Saved'))[0]).toBeInTheDocument()
  })

  it('saves a note when the field loses focus, without waiting', async () => {
    renderApp('/checklist')
    const box = await screen.findByLabelText('Notes for D1', {}, T)
    await userEvent.type(box, 'ok')
    fireEvent.blur(box)
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledWith('run4', 'D1', { note: 'ok' }))
  })

  it('shows the zip button only when the run has screenshots, with the count', async () => {
    renderApp('/checklist')
    expect(await screen.findByLabelText('Notes for D2', {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /screenshots? \(zip\)/ })).toBeNull()
  })

  it('downloads the screenshots as one zip and says so', async () => {
    m.loadRun.mockResolvedValue({ path: PATH, images_path: `${PATH}/images`, notes: {
      B1: { status: 'pass', note: '', images: ['B1-0123456789ab.png', 'B1-aaaaaaaaaaaa.png'] },
      C5: { status: 'pass', note: '', images: ['C5-0123456789ab.png'] },
    } })
    m.downloadScreenshots.mockResolvedValue(undefined)
    renderApp('/checklist')
    await userEvent.click(await screen.findByRole('button', { name: 'Download 3 screenshots (zip)' }, T))
    expect(m.downloadScreenshots).toHaveBeenCalledWith('run4')
    expect(await screen.findByText('Zip downloaded')).toBeInTheDocument()
  })

  it('says why the zip failed in plain words', async () => {
    m.loadRun.mockResolvedValue({ path: PATH, images_path: `${PATH}/images`, notes: { B1: { status: 'pass', note: '', images: ['B1-0123456789ab.png'] } } })
    m.downloadScreenshots.mockRejectedValue(new testrun.TestrunError(409, 'zip.too_large'))
    renderApp('/checklist')
    await userEvent.click(await screen.findByRole('button', { name: 'Download 1 screenshot (zip)' }, T))
    expect(await screen.findByText(/larger than 200 MB/)).toBeInTheDocument()
  })

  it('shows what was saved before and filters to steps with notes or failures', async () => {
    m.loadRun.mockResolvedValue({ path: PATH, images_path: `${PATH}/images`, notes: {
      C3: { status: 'fail_blocking', note: '', images: [] },
      D2: { status: 'pass', note: 'took long', images: [] },
      D3: { status: 'pass', note: '', images: [] },
    } })
    renderApp('/checklist')
    expect(await screen.findByLabelText('Notes for D2', {}, T)).toHaveValue('took long')
    await userEvent.click(screen.getByLabelText('Only steps with notes or failures'))
    expect(screen.getByRole('heading', { name: 'Edit a ticket' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Agent verbs need a grant for your shell' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Human approval (no grant)' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Install with the line from the lead' })).toBeNull()
  })

  it('adds a pasted screenshot, shows it with its path, and removes it', async () => {
    m.addImage.mockResolvedValue({ name: 'C5-0123456789ab.png', path: `${PATH}/images/C5-0123456789ab.png` })
    renderApp('/checklist')
    const box = await screen.findByLabelText('Notes for C5', {}, T)
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' })
    fireEvent.paste(box, { clipboardData: { files: [file] } })
    await waitFor(() => expect(m.addImage).toHaveBeenCalledWith('run4', 'C5', file))
    expect(await screen.findByText(`${PATH}/images/C5-0123456789ab.png`)).toBeInTheDocument()
    expect(await screen.findByAltText('Screenshot C5-0123456789ab.png')).toHaveAttribute('src', 'data:image/png;base64,AAAA')
    m.removeImage.mockResolvedValue(undefined)
    await userEvent.click(screen.getByRole('button', { name: 'Remove screenshot C5-0123456789ab.png' }))
    await waitFor(() => expect(screen.queryByAltText('Screenshot C5-0123456789ab.png')).toBeNull())
    expect(m.removeImage).toHaveBeenCalledWith('run4', 'C5-0123456789ab.png')
  })

  it('refuses a wrong type or an oversized picture before sending it', async () => {
    renderApp('/checklist')
    const input = await screen.findByLabelText('Add screenshot to C5', {}, T)
    // the file input only offers images; the drop path filters the same way, and the size check names the reason
    const big = new File([new Uint8Array(10)], 'big.png', { type: 'image/png' })
    Object.defineProperty(big, 'size', { value: 6 * 1024 * 1024 })
    fireEvent.change(input, { target: { files: [big] } })
    expect(await screen.findByText('That picture is larger than 5 MB.')).toBeInTheDocument()
    expect(m.addImage).not.toHaveBeenCalled()
    expect(testrun.imageProblem({ type: 'image/gif', size: 5 })).toMatch(/Only PNG/)
  })

  it('says so when the session ended, and keeps what was typed', async () => {
    m.saveStep.mockRejectedValue(new testrun.TestrunError(401, 'session.required'))
    renderApp('/checklist')
    const box = await screen.findByLabelText('Notes for C2', {}, T)
    await userEvent.type(box, 'abc')
    fireEvent.blur(box)
    expect(await screen.findByRole('alert')).toHaveTextContent(/orch dashboard open \/checklist/)
    expect(box).toHaveValue('abc')
  })

  it('works when the server is unreachable (still shows every step)', async () => {
    m.loadRun.mockRejectedValue(new Error('down'))
    renderApp('/checklist')
    expect(await screen.findByRole('heading', { name: 'Install with the line from the lead' }, T)).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('copies a markdown report of the marked steps', async () => {
    m.loadRun.mockResolvedValue({ path: PATH, images_path: `${PATH}/images`, notes: {
      C3: { status: 'fail_blocking', note: 'KeyError', images: ['C3-aaaaaaaaaaaa.png'] },
    } })
    const user = userEvent.setup()
    renderApp('/checklist')
    await screen.findByLabelText('Notes for C3', {}, T)
    await user.click(screen.getByRole('button', { name: /Copy report/ }))
    const text = await navigator.clipboard.readText()
    expect(text).toContain('## C3 Edit a ticket: Failed, blocking')
    expect(text).toContain('KeyError')
    expect(text).toContain(`- ${PATH}/images/C3-aaaaaaaaaaaa.png`)
    expect(text).toContain(`Notes saved in: ${PATH}`)
    expect(text).not.toContain('## C1')
  })
})

describe('first save', () => {
  it('keeps edits made before the first load finished, and sends only the changed field', async () => {
    let release!: (v: Awaited<ReturnType<typeof testrun.loadRun>>) => void
    m.loadRun.mockReturnValue(new Promise((r) => (release = r)))
    renderApp('/checklist')
    const group = await screen.findByRole('radiogroup', { name: 'Result of A2' }, T)
    await userEvent.click(within(group).getByRole('radio', { name: 'Pass' }))
    await userEvent.type(screen.getByLabelText('Notes for B1.1'), 'n')
    release({ path: PATH, images_path: `${PATH}/images`, notes: { A2: { status: 'todo', note: 'server note', images: ['A2-aaaaaaaaaaaa.png'] } } })
    await waitFor(() => expect(screen.getByLabelText('Notes for A2')).toHaveValue('server note'))
    expect(within(screen.getByRole('radiogroup', { name: 'Result of A2' })).getByRole('radio', { name: 'Pass' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('Notes for B1.1')).toHaveValue('n')
    expect(await screen.findByAltText('Screenshot A2-aaaaaaaaaaaa.png')).toBeInTheDocument()
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledWith('run4', 'A2', { status: 'pass', hash: stepHash(ALL_STEPS.find((x) => x.id === 'A2')!) }))
    expect(m.saveStep).not.toHaveBeenCalledWith('run4', 'A2', expect.objectContaining({ note: expect.anything() }))
  })
})

describe('a save that fails once', () => {
  it('is sent again, so a mark is never lost while the note and the picture are saved (first-use loss)', async () => {
    // The status request fails once (a stale connection); the note saved a moment later must not make it look saved.
    m.saveStep.mockRejectedValueOnce(new TypeError('network error'))
    renderApp('/checklist')
    const group = await screen.findByRole('radiogroup', { name: 'Result of A2' }, T)
    await userEvent.click(within(group).getByRole('radio', { name: 'Pass' }))
    await userEvent.type(screen.getByLabelText('Notes for A2'), 'x')
    await waitFor(() => expect(m.saveStep.mock.calls.filter((c) => c[2].status === 'pass').length).toBeGreaterThanOrEqual(2), T)
    expect(m.saveStep.mock.calls.at(-1)).toEqual(['run4', 'A2', expect.objectContaining({ status: 'pass' })])
    expect((await screen.findAllByText('Saved', {}, T))[0]).toBeInTheDocument()
  })
  it('keeps trying while the server answers nothing, and says so', async () => {
    m.saveStep.mockRejectedValue(new TypeError('network error'))
    renderApp('/checklist')
    const group = await screen.findByRole('radiogroup', { name: 'Result of A2' }, T)
    await userEvent.click(within(group).getByRole('radio', { name: 'Pass' }))
    expect(await screen.findByText('Not saved', {}, T)).toBeInTheDocument()
    await waitFor(() => expect(m.saveStep.mock.calls.length).toBeGreaterThanOrEqual(2), T)
    m.saveStep.mockResolvedValue(undefined)
  })
})

describe('buildReport', () => {
  it('keeps notes of a step that is no longer in the list', () => {
    const out = buildReport({ H9: { status: 'fail_blocking', note: 'old note', images: [] } }, '/i', '/p')
    expect(out).toContain('## H9 (not in the current list): Failed, blocking')
    expect(out).toContain('old note')
  })
  it('says nothing is marked when nothing is', () => {
    expect(buildReport({}, '/i', '/p')).toContain('Nothing marked yet.')
  })
})

describe('F8 retry rules', () => {
  it('does not resend a save the server refused (400/403/404/422) and says so in plain words', async () => {
    m.saveStep.mockReset()
    m.saveStep.mockRejectedValue(new testrun.TestrunError(400, 'invalid.input'))
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      renderApp('/checklist')
      const group = await screen.findByRole('radiogroup', { name: 'Result of A2' }, T)
      await userEvent.click(within(group).getByRole('radio', { name: 'Pass' }))
      expect(await screen.findByText(/did not accept this change/, {}, T)).toBeInTheDocument()
      await vi.advanceTimersByTimeAsync(60_000) // far past every retry delay
      expect(m.saveStep).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
  it('a refused field is not sent again with the next edit of the step (only the new field goes)', async () => {
    m.saveStep.mockReset()
    m.saveStep.mockRejectedValueOnce(new testrun.TestrunError(422, 'invalid.input')).mockResolvedValue(undefined)
    renderApp('/checklist')
    const group = await screen.findByRole('radiogroup', { name: 'Result of A2' }, T)
    await userEvent.click(within(group).getByRole('radio', { name: 'Pass' }))
    await screen.findByText(/did not accept this change/, {}, T)
    await userEvent.type(screen.getByLabelText('Notes for A2'), 'n')
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledTimes(2), T)
    expect(m.saveStep).toHaveBeenLastCalledWith(RUN_ID, 'A2', { note: 'n' })
  })
  it('resends after a 503', async () => {
    m.saveStep.mockRejectedValueOnce(new testrun.TestrunError(503, 'host.unavailable'))
    renderApp('/checklist')
    const group = await screen.findByRole('radiogroup', { name: 'Result of A2' }, T)
    await userEvent.click(within(group).getByRole('radio', { name: 'Pass' }))
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledTimes(2), T)
  })
})

describe('keepalive only for small bodies', () => {
  it('a 20,000-character non-ASCII note (60 KB in UTF-8) is sent with a normal fetch, a short one with keepalive', async () => {
    const real = await vi.importActual<typeof import('@/api/testrun')>('@/api/testrun')
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      await real.saveStep('run4', 'A2', { note: '€'.repeat(20000) })
      await real.saveStep('run4', 'A2', { note: 'short ü' })
      const calls = fetchMock.mock.calls as unknown as [string, { keepalive: boolean }][]
      expect(calls[0][1].keepalive).toBe(false)
      expect(calls[1][1].keepalive).toBe(true)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('carry-over from an earlier run', () => {
  const A1 = ALL_STEPS.find((x) => x.id === 'A1')!
  const A2 = ALL_STEPS.find((x) => x.id === 'A2')!
  const old = (id: string, status: testrun.StepStatus, extra: Partial<testrun.PreviousRun['steps'][string]> = {}) => ({
    [id]: { status, note: '', images: 0, updated: '2026-10-19T10:00:00+00:00', passed_at: '2026-10-19T10:00:00+00:00', hash: null, ...extra },
  })
  const prev = (steps: testrun.PreviousRun['steps']): testrun.PreviousRun[] => [{ run: 'run2', steps }]

  it('pure rules: same text is done, changed text is not, failures give context, a reopened or touched step is left alone', () => {
    const same = prev(old('A1', 'pass', { hash: stepHash(A1) }))
    expect(carryFor(A1, undefined, same)).toMatchObject({ kind: 'done', run: 'run2' })
    expect(carryFor(A1, undefined, prev(old('A1', 'pass', { hash: '0000000000000000' })))).toEqual({ kind: 'changed', run: 'run2' })
    expect(carryFor(A1, undefined, prev(old('A1', 'pass')))).toMatchObject({ kind: 'done', run: 'run2' })
    expect(carryFor({ ...A1, text: 'different' }, undefined, same)).toMatchObject({ kind: 'changed' })
    expect(carryFor(A1, { status: 'todo', note: '', images: [], retest: true }, same)).toBeNull()
    expect(carryFor(A1, { status: 'fail_blocking', note: '', images: [] }, same)).toBeNull()
    expect(carryFor(A1, undefined, prev(old('A1', 'fail_blocking', { note: 'broke' })))).toEqual({ kind: 'context', run: 'run2', status: 'fail_blocking', note: 'broke' })
    expect(carryFor(A1, undefined, prev(old('A1', 'todo')))).toBeNull()
    // the newest run that has anything wins
    const two: testrun.PreviousRun[] = [{ run: 'run1', steps: old('A1', 'pass', { hash: stepHash(A1) }) }, { run: 'run2', steps: old('A1', 'fail_non_blocking', { note: 'x' }) }]
    expect(carryFor(A1, undefined, two)).toMatchObject({ kind: 'context', run: 'run2' })
  })

  it('shows a passed step collapsed as done with its date, counts it, and Test again reopens it', async () => {
    m.loadPrevious.mockResolvedValue(prev(old('A1', 'pass', { hash: stepHash(A1) })))
    renderApp('/checklist')
    expect(await screen.findByText(/Done in run 2 \(19 Oct 2026\)/, {}, T)).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup', { name: 'Result of A1' })).toBeNull()
    expect(screen.getByText(/1 pass \(1 done earlier\)/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Test again: A1/ }))
    expect(m.saveStep).toHaveBeenCalledWith('run4', 'A1', { retest: true })
    expect(await screen.findByRole('radiogroup', { name: 'Result of A1' })).toBeInTheDocument()
    expect(screen.queryByText(/Done in run 2/)).toBeNull()
  })

  it('a changed step says so, a failed one shows its old note, and the report lists carried steps', async () => {
    m.loadPrevious.mockResolvedValue(prev({ ...old('A1', 'pass', { hash: 'ffffffffffffffff' }), ...old('A2', 'fail_blocking', { note: 'blank page' }) }))
    renderApp('/checklist')
    expect(await screen.findByText(/Changed since run 2, please test again/, {}, T)).toBeInTheDocument()
    expect(screen.getByText('blank page')).toBeInTheDocument()
    expect(screen.getByRole('radiogroup', { name: 'Result of A2' })).toBeInTheDocument()
    const report = buildReport({}, '/i', '/p', { A1: { kind: 'done', run: 'run2', date: '2026-10-19T10:00:00+00:00' } })
    expect(report).toContain(`## A1 ${A1.title}: Pass (done in run 2, 19 Oct 2026, not tested again)`)
    expect(report).toContain('Pass: 1')
    expect(A2.id).toBe('A2')
  })

  it('a pass saved without a fingerprint gets one on load, and a failing earlier-run load is ignored', async () => {
    m.loadRun.mockResolvedValue({ path: PATH, images_path: `${PATH}/images`, notes: { A1: { status: 'pass', note: '', images: [] } } })
    m.loadPrevious.mockRejectedValue(new Error('x'))
    renderApp('/checklist')
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledWith('run4', 'A1', { hash: stepHash(A1) }))
    expect(screen.getAllByRole('radiogroup')).toHaveLength(ALL_STEPS.length)
  })
})
