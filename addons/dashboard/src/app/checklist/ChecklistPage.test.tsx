// The tester's checklist page: every step of run 2, status/notes/screenshots with autosave, the filter and the report.
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderApp } from '@/test/renderApp'
import * as testrun from '@/api/testrun'
import { ALL_STEPS, INSTALL_PIN, RUN2_SECTIONS } from './run2'
import { buildReport } from './report'

vi.mock('@/api/testrun', async (orig) => {
  const real = await orig<typeof import('@/api/testrun')>()
  return { ...real, loadRun: vi.fn(), saveStep: vi.fn(), addImage: vi.fn(), removeImage: vi.fn(), imageUrl: vi.fn() }
})
const m = vi.mocked(testrun)
const T = { timeout: 8000 }
const PATH = '/Users/x/.config/orch/testrun/run2'

beforeEach(() => {
  vi.clearAllMocks()
  m.loadRun.mockResolvedValue({ path: PATH, images_path: `${PATH}/images`, notes: {} })
  m.saveStep.mockResolvedValue(undefined)
  m.imageUrl.mockResolvedValue('data:image/png;base64,AAAA')
})

describe('run 2 data', () => {
  it('has unique ids the server accepts, and every section of the source file', () => {
    const ids = ALL_STEPS.map((s) => s.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const id of ids) expect(id).toMatch(/^[A-G][0-9]{1,2}(\.[0-9]{1,2})?$/)
    expect(RUN2_SECTIONS.map((s) => s.id)).toEqual(['A', 'B', 'C', 'D', 'E', 'F'])
    for (const id of ['A1', 'A2', 'B1.1', 'B1.7', 'B2.8', 'B3.9', 'C1', 'C8', 'D6', 'E6', 'F8']) expect(ids).toContain(id)
  })
  it('pins A1 to a full commit and holds no secret material', () => {
    expect(INSTALL_PIN).toMatch(/^[0-9a-f]{40}$/)
    const a1 = ALL_STEPS.find((s) => s.id === 'A1')!
    expect(a1.commands![0]).toContain(`orch-core@${INSTALL_PIN}#subdirectory=addons/dashboard/server`)
    const all = JSON.stringify(RUN2_SECTIONS)
    expect(all).not.toMatch(/BEGIN [A-Z ]*PRIVATE KEY|ORCH_GRANT=[A-Za-z0-9_-]{16,}|\.p8/)
  })
})

describe('the checklist page', () => {
  it('shows the steps, the known gaps and where the notes are saved, without a workspace session call', async () => {
    renderApp('/checklist')
    expect(await screen.findByRole('heading', { name: /Test checklist/ }, T)).toBeInTheDocument()
    expect(await screen.findByText(PATH, {}, T)).toBeInTheDocument()
    expect(screen.getByText(/Your notes are saved in/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /Known gaps/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Install with one line' })).toBeInTheDocument()
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
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledWith('run2', 'C1', 'fail_blocking', ''))
    await userEvent.type(screen.getByLabelText('Notes for C1'), 'blank page')
    await waitFor(() => expect(m.saveStep).toHaveBeenLastCalledWith('run2', 'C1', 'fail_blocking', 'blank page'), T)
    expect((await screen.findAllByText('Saved'))[0]).toBeInTheDocument()
  })

  it('saves a note when the field loses focus, without waiting', async () => {
    renderApp('/checklist')
    const box = await screen.findByLabelText('Notes for D1', {}, T)
    await userEvent.type(box, 'ok')
    fireEvent.blur(box)
    await waitFor(() => expect(m.saveStep).toHaveBeenCalledWith('run2', 'D1', 'todo', 'ok'))
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
    expect(screen.queryByRole('heading', { name: 'Install with one line' })).toBeNull()
  })

  it('adds a pasted screenshot, shows it with its path, and removes it', async () => {
    m.addImage.mockResolvedValue({ name: 'C5-0123456789ab.png', path: `${PATH}/images/C5-0123456789ab.png` })
    renderApp('/checklist')
    const box = await screen.findByLabelText('Notes for C5', {}, T)
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' })
    fireEvent.paste(box, { clipboardData: { files: [file] } })
    await waitFor(() => expect(m.addImage).toHaveBeenCalledWith('run2', 'C5', file))
    expect(await screen.findByText(`${PATH}/images/C5-0123456789ab.png`)).toBeInTheDocument()
    expect(await screen.findByAltText('Screenshot C5-0123456789ab.png')).toHaveAttribute('src', 'data:image/png;base64,AAAA')
    m.removeImage.mockResolvedValue(undefined)
    await userEvent.click(screen.getByRole('button', { name: 'Remove screenshot C5-0123456789ab.png' }))
    await waitFor(() => expect(screen.queryByAltText('Screenshot C5-0123456789ab.png')).toBeNull())
    expect(m.removeImage).toHaveBeenCalledWith('run2', 'C5-0123456789ab.png')
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
    expect(await screen.findByRole('heading', { name: 'Install with one line' }, T)).toBeInTheDocument()
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

describe('buildReport', () => {
  it('says nothing is marked when nothing is', () => {
    expect(buildReport({}, '/i', '/p')).toContain('Nothing marked yet.')
  })
})
