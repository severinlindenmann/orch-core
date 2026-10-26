import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { runsApi, type Run } from '@/api/runs'
import { RealTerminal } from './RealTerminal'
import { TermTools } from './TermTools'
import * as termView from './termView'
import type { TermConnection, TermHandlers } from './termProtocol'

const run = { run: 'demo-run', state: 'running', harness: 'shell', label: 'My shell', writer: false } as Run
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('disables tools before the view exists', () => {
  render(<TermTools view={null} run={run} finding={false} onFinding={vi.fn()} />)
  for (const button of screen.getAllByRole('button')) expect(button).toBeDisabled()
})

it.each([false, true])('finds, copies and downloads without socket input (writer=%s)', async (writer) => {
  const user = userEvent.setup()
  const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
  const create = vi.fn((_blob: Blob) => 'blob:demo-transcript')
  const revoke = vi.fn()
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }))
  let downloaded = ''
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { downloaded = this.download })
  const make = vi.spyOn(termView, 'createTermView')
  vi.spyOn(runsApi, 'ticket').mockResolvedValue({ ticket: 'demo-not-a-real-ticket', expires_in_ms: 30000 })
  const socket = { sendInput: vi.fn(), resize: vi.fn(), close: vi.fn() }
  let handlers!: TermHandlers
  render(<RealTerminal ws="demo" run={run} initial="read" connect={(_ticket, h) => { handlers = h; return socket as TermConnection }} />)
  await waitFor(() => expect(handlers).toBeDefined())
  act(() => handlers.ready({ start: 0, offset: 0, cols: 80, rows: 24, writer, state: 'running' }))
  const view = make.mock.results[0].value as termView.TermView
  await act(async () => { await new Promise<void>((resolve) => view.term.write('hello world\r\nsecond line\r\n', resolve)) })
  if (!writer) expect(screen.getByText('Read only.')).toBeInTheDocument()
  // Ctrl+F is a shell key (forward-char): only Cmd+F opens Find.
  fireEvent.keyDown(screen.getByLabelText('Terminal input'), { key: 'f', ctrlKey: true })
  expect(screen.queryByRole('textbox', { name: 'Find in terminal' })).not.toBeInTheDocument()
  fireEvent.keyDown(screen.getByLabelText('Terminal input'), { key: 'f', metaKey: true })
  expect(screen.getByRole('textbox', { name: 'Find in terminal' })).toHaveFocus()
  await user.click(screen.getByRole('button', { name: 'Close find' }))
  await user.click(screen.getByRole('button', { name: 'Find' }))
  const input = screen.getByRole('textbox', { name: 'Find in terminal' })
  await user.type(input, 'hello{Enter}')
  expect(screen.getByText('Match found')).toHaveAttribute('role', 'status')
  await user.clear(input)
  await user.type(input, 'absent{Enter}')
  expect(screen.getByText('No matches')).toHaveAttribute('role', 'status')
  act(() => view.term.select(0, 0, 5))
  await user.click(screen.getByRole('button', { name: 'Copy' }))
  expect(writeText).toHaveBeenLastCalledWith('hello')
  expect(screen.getByText('Copied')).toBeInTheDocument()
  act(() => view.term.clearSelection())
  await user.click(screen.getByRole('button', { name: 'Copy' }))
  expect(writeText).toHaveBeenLastCalledWith('hello world\nsecond line')
  writeText.mockRejectedValueOnce(new Error('denied'))
  await user.click(screen.getByRole('button', { name: 'Copy' }))
  expect(screen.getByText('Could not copy')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Download transcript' }))
  expect(downloaded).toMatch(/^my-shell-\d{8}-\d{4}\.txt$/)
  const blob = create.mock.calls[0][0] as Blob
  expect(blob.type).toBe('text/plain;charset=utf-8')
  const text = await new Promise((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(blob) })
  expect(text).toBe('hello world\nsecond line')
  await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:demo-transcript'))
  expect(socket.sendInput).not.toHaveBeenCalled()
  expect(runsApi.ticket).toHaveBeenCalledTimes(1)
})
