import { act, screen, waitFor, within } from '@testing-library/react'
import { toast } from 'sonner'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { SAMPLE_TRANSCRIPTS } from './quickRules'

const T = { timeout: 5000 }

async function openOverlay(path = '/board') {
  const r = renderApp(path)
  await waitFor(() => expect(screen.getByTestId('topbar-title')).not.toHaveTextContent(/^$/), T)
  const button = await screen.findByRole('button', { name: /New ticket/ }, T)
  await r.user.click(button)
  const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
  await within(sheet).findByLabelText('Title', {}, T)
  return { ...r, sheet, button }
}

afterEach(() => vi.restoreAllMocks())

describe('new ticket overlay', () => {
  it('opens over the page with the quick line focused and the optional sections folded', async () => {
    const { sheet, user } = await openOverlay()
    expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board')
    expect(within(sheet).getByRole('textbox', { name: 'Quick ticket' })).toHaveFocus()
    expect(within(sheet).getByLabelText(/^Requirements/)).toBeInTheDocument()
    expect(within(sheet).queryByLabelText('Context')).toBeNull()
    const more = within(sheet).getByRole('button', { name: /More sections/ })
    expect(more).toHaveAttribute('aria-expanded', 'false')
    await user.click(more)
    expect(within(sheet).getByLabelText('Context')).toBeInTheDocument()
    expect(within(sheet).getByRole('heading', { name: 'Acceptance criteria' })).toBeInTheDocument()
  })

  it('a quick ticket is one line and Enter: a backlog bug with the text as requirements, a toast with Open, focus back', async () => {
    const success = vi.spyOn(toast, 'success')
    const { sheet, user, button } = await openOverlay()
    const line = within(sheet).getByRole('textbox', { name: 'Quick ticket' })
    await user.type(line, 'Login button is broken on Safari. It does nothing.')
    expect(within(sheet).getByText(/Enter creates a/)).toHaveTextContent('Enter creates a bug in Backlog: “Login button is broken on Safari”')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull(), T)
    expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board')
    expect(success).toHaveBeenCalledTimes(1)
    const [title, opts] = success.mock.calls.at(-1)!
    expect(title).toMatch(/^Created DEMO-\d+ · bug$/)
    expect(opts).toMatchObject({ id: /DEMO-\d+/.exec(String(title))![0], cancel: { label: 'Undo' } })
    const key = /DEMO-\d+/.exec(String(title))![0]
    const ticket = mockStore.ticket(key)!
    expect(ticket.status).toBe('backlog')
    expect(ticket.body.requirements).toBe('Login button is broken on Safari. It does nothing.')
    await waitFor(() => expect(button).toHaveFocus(), T)
    expect(mockStore.hasTicket(key)).toBe(true)
    // Open takes the person to the ticket.
    act(() => (opts as unknown as { action: { onClick: () => void } }).action.onClick())
    expect(await screen.findByRole('heading', { level: 1, name: /Login button is broken on Safari/ }, T)).toBeInTheDocument()
  })

  it('refuses a quick line that is too short, saying why', async () => {
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByRole('textbox', { name: 'Quick ticket' }), 'Refactoring{Enter}')
    expect(within(sheet).getByText('Write at least two words.')).toBeInTheDocument()
    expect(within(sheet).getByRole('textbox', { name: 'Quick ticket' })).toHaveAttribute('aria-invalid', 'true')
  })

  it('asks before discarding typed text (Esc), and Discard clears the draft', async () => {
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByLabelText('Title'), 'Half done')
    await user.keyboard('{Escape}')
    expect(await screen.findByText('Discard unsaved changes?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(screen.getByRole('dialog', { name: 'New ticket' })).toBeInTheDocument()
    expect(within(screen.getByRole('dialog', { name: 'New ticket' })).getByLabelText('Title')).toHaveValue('Half done')
    await user.click(within(screen.getByRole('dialog', { name: 'New ticket' })).getByRole('button', { name: 'Cancel' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull())
    expect(Object.keys(localStorage).filter((k) => k.includes('new-ticket.draft'))).toHaveLength(0)
  })

  it('a route change while something is typed asks first', async () => {
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByRole('textbox', { name: 'Quick ticket' }), 'Something to keep')
    await user.keyboard('{Control>}k{/Control}')
    await user.click(await screen.findByRole('option', { name: /Go to Tickets/ }))
    expect(await screen.findByText('Discard unsaved changes?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Tickets'))
    expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull()
  })

  it('closes without asking when nothing is typed', async () => {
    const { user } = await openOverlay()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull())
    expect(screen.queryByText('Discard unsaved changes?')).toBeNull()
  })

  it('Open full page carries the draft to /tickets/new without the "restored" note', async () => {
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByLabelText('Title'), 'Long one')
    await user.type(within(sheet).getByLabelText(/^Requirements/), 'Needs room')
    await user.type(within(sheet).getByRole('textbox', { name: 'Quick ticket' }), 'Also export CSV')
    await user.click(within(sheet).getAllByRole('button', { name: 'Open full page' })[0])
    expect(await screen.findByRole('heading', { level: 1, name: 'New ticket' }, T)).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull()
    expect(screen.getByLabelText('Title')).toHaveValue('Long one')
    // The quick line is never dropped: it is appended to the requirements.
    expect(screen.getByLabelText(/^Requirements/)).toHaveValue('Needs room\n\nAlso export CSV')
    expect(screen.queryByText(/Draft restored/)).toBeNull()
  })

  it('"Close, keep draft" closes and the draft is there next time', async () => {
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByLabelText('Title'), 'Keep me')
    await user.keyboard('{Escape}')
    await user.click(await screen.findByRole('button', { name: 'Close, keep draft' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull())
    await user.click(screen.getByRole('button', { name: /New ticket/ }))
    const again = await screen.findByRole('dialog', { name: 'New ticket' }, T)
    expect(await within(again).findByLabelText('Title')).toHaveValue('Keep me')
    expect(within(again).getByText(/Draft restored/)).toBeInTheDocument()
  })

  it('only the quick line typed: the prompt offers no "keep draft"', async () => {
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByRole('textbox', { name: 'Quick ticket' }), 'Two words')
    await user.keyboard('{Escape}')
    expect(await screen.findByText('Discard unsaved changes?')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Close, keep draft' })).toBeNull()
  })

  it('from the palette, New ticket puts the focus in the quick line', async () => {
    const { user } = renderApp('/board')
    await waitFor(() => expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board'), T)
    await user.keyboard('{Control>}k{/Control}')
    await user.click(await screen.findByRole('option', { name: /^New ticket/ }))
    const sheet = await screen.findByRole('dialog', { name: 'New ticket' }, T)
    await waitFor(() => expect(within(sheet).getByRole('textbox', { name: 'Quick ticket' })).toHaveFocus(), T)
  })

  it('the full form in the overlay creates the ticket and stays on the page', async () => {
    const success = vi.spyOn(toast, 'success')
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByLabelText('Title'), 'Overlay form ticket')
    await user.type(within(sheet).getByLabelText(/^Requirements/), 'Made in the sheet')
    await user.click(within(sheet).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New ticket' })).toBeNull(), T)
    expect(screen.getByTestId('topbar-title')).toHaveTextContent('Board')
    const title = String(success.mock.calls.at(-1)![0])
    expect(title).toMatch(/^Created DEMO-\d+ · feature$/)
    expect(mockStore.ticket(/DEMO-\d+/.exec(title)![0])?.title).toBe('Overlay form ticket')
  })

  it('an empty Create in the overlay focuses the title with its error', async () => {
    const { sheet, user } = await openOverlay()
    await user.click(within(sheet).getByRole('button', { name: 'Create' }))
    expect(await within(sheet).findByText('The title needs 3 to 120 characters.')).toBeInTheDocument()
    expect(within(sheet).getByLabelText('Title')).toHaveFocus()
  })
})

describe('dictation (simulated)', () => {
  it('records with a timer and a meter, never opens the microphone, and Stop fills a transcription', async () => {
    const getUserMedia = vi.fn()
    Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
    const { sheet, user } = await openOverlay()
    await user.click(within(sheet).getByRole('button', { name: 'Dictate (simulated)' }))
    const rec = within(sheet).getByRole('group', { name: 'Dictation' })
    expect(within(rec).getByText('Simulated — no audio leaves your browser')).toBeInTheDocument()
    expect(within(rec).getByText('0:00')).toBeInTheDocument()
    expect(within(rec).getByTestId('level-meter')).toBeInTheDocument()
    await user.click(within(rec).getByRole('button', { name: 'Stop' }))
    const line = within(sheet).getByRole('textbox', { name: 'Quick ticket' })
    expect(SAMPLE_TRANSCRIPTS).toContain((line as HTMLInputElement).value)
    expect(line).toHaveFocus()
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('Cancel drops the recording', async () => {
    const { sheet, user } = await openOverlay()
    await user.click(within(sheet).getByRole('button', { name: 'Dictate (simulated)' }))
    await user.click(within(within(sheet).getByRole('group', { name: 'Dictation' })).getByRole('button', { name: 'Cancel' }))
    expect(within(sheet).getByRole('textbox', { name: 'Quick ticket' })).toHaveValue('')
    expect(within(sheet).getByRole('textbox', { name: 'Quick ticket' })).toHaveFocus()
  })

  it('Esc while recording stops it and keeps the sheet open', async () => {
    const { sheet, user } = await openOverlay()
    await user.click(within(sheet).getByRole('button', { name: 'Dictate (simulated)' }))
    await user.keyboard('{Escape}')
    expect(screen.getByRole('dialog', { name: 'New ticket' })).toBeInTheDocument()
    expect(within(sheet).queryByRole('group', { name: 'Dictation' })).toBeNull()
    expect(SAMPLE_TRANSCRIPTS).toContain((within(sheet).getByRole('textbox', { name: 'Quick ticket' }) as HTMLInputElement).value)
  })

  it('holds the meter still under prefers-reduced-motion', async () => {
    vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({ matches: q.includes('reduce'), media: q, onchange: null, addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false }) as MediaQueryList)
    const { sheet, user } = await openOverlay()
    await user.click(within(sheet).getByRole('button', { name: 'Dictate (simulated)' }))
    expect(within(sheet).getByTestId('level-meter')).toHaveAttribute('data-still', 'true')
  })
})

describe('Quick ticket in the palette', () => {
  it('creates a backlog ticket from one line', async () => {
    const success = vi.spyOn(toast, 'success')
    const { user } = renderApp('/')
    await screen.findByRole('heading', { name: 'Today' }, T)
    await user.keyboard('{Control>}k{/Control}')
    await user.click(await screen.findByRole('option', { name: /Quick ticket…/ }))
    await user.keyboard('Investigate the slow nightly import')
    expect(await screen.findByRole('option', { name: /Create spike in Backlog: “Investigate the slow nightly import”/ })).toBeInTheDocument()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(success.mock.calls.at(-1)?.[0]).toMatch(/^Created DEMO-\d+ · spike$/), T)
    expect(success).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('heading', { name: 'Today' })).toBeInTheDocument()
  })

  it('a viewer sees the reason instead', async () => {
    const { user } = renderApp('/', { viewer: 'p_tom' })
    await screen.findByRole('heading', { name: 'Today' }, T)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Viewing as' })).toHaveTextContent(/viewer/))
    await user.keyboard('{Control>}k{/Control}')
    const opt = await screen.findByRole('option', { name: /Quick ticket…/ })
    expect(opt).toHaveAttribute('aria-disabled', 'true')
    expect(opt).toHaveTextContent('Viewers cannot create tickets')
  })
})

describe('Undo on the created toast', () => {
  it('removes the quick ticket', async () => {
    const success = vi.spyOn(toast, 'success')
    const { sheet, user } = await openOverlay()
    await user.type(within(sheet).getByRole('textbox', { name: 'Quick ticket' }), 'Bump vite to 8{Enter}')
    await waitFor(() => expect(success).toHaveBeenCalledTimes(1), T)
    const [title, opts] = success.mock.calls[0]
    const key = /DEMO-\d+/.exec(String(title))![0]
    expect(mockStore.hasTicket(key)).toBe(true)
    await act(async () => (opts as unknown as { cancel: { onClick: () => Promise<void> } }).cancel.onClick())
    expect(mockStore.hasTicket(key)).toBe(false)
    expect(success.mock.calls.at(-1)![0]).toBe(`Removed ${key}`)
  })
})
