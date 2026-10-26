import { fireEvent, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 8000 }
beforeEach(() => localStorage.clear())

describe('feedback page', () => {
  it('opens a next field after one is filled, and numbers a sub-feedback under it', async () => {
    renderApp('/feedback')
    const first = await screen.findByLabelText('Feedback 1', undefined, T)
    expect(screen.queryByLabelText('Feedback 2')).toBeNull()
    await userEvent.type(first, 'the terminal is cut off')
    expect(await screen.findByLabelText('Feedback 2')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /sub-feedback/ }))
    const sub = await screen.findByLabelText('Feedback 1.1')
    await userEvent.type(sub, 'only on mobile')
    expect(await screen.findByLabelText('Feedback 1.2')).toBeInTheDocument()
    expect(screen.getByTestId('fb-count')).toHaveTextContent('2 items')
    expect(screen.getByTestId('fb-round').textContent).toMatch(/^round \d{8}T\d{6}Z$/)
  })
  it('adds a pasted screenshot and shows the report with its image link', async () => {
    renderApp('/feedback')
    const first = await screen.findByLabelText('Feedback 1', undefined, T)
    await userEvent.type(first, 'see picture')
    const file = new File([new Uint8Array([137, 80, 78, 71])], 'x.png', { type: 'image/png' })
    fireEvent.paste(first, { clipboardData: { items: [{ type: 'image/png', getAsFile: () => file }] } })
    expect(await screen.findByAltText('Screenshot 1 of feedback 1')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /Copy report/ }))
    expect(await screen.findByLabelText('Report preview')).toHaveTextContent(/fb1-img1\.png/)
  })
  it('numbers like the report when a middle item is emptied again', async () => {
    renderApp('/feedback')
    await userEvent.type(await screen.findByLabelText('Feedback 1', undefined, T), 'a')
    await userEvent.type(await screen.findByLabelText('Feedback 2'), 'b')
    await userEvent.type(await screen.findByLabelText('Feedback 3'), 'c')
    await userEvent.clear(screen.getByLabelText('Feedback 2'))
    expect(await screen.findByLabelText('Feedback –')).toBeInTheDocument()
    expect(screen.getByLabelText('Feedback 2')).toHaveValue('c')
    expect(screen.getByLabelText('Feedback 3')).toHaveValue('')
  })
  it('survives a stored value of the wrong shape', async () => {
    localStorage.setItem('orch-feedback', JSON.stringify({ items: [{ id: 1, text: 'old' }], uid: 1 }))
    renderApp('/feedback', { storage: { 'orch-feedback': JSON.stringify({ items: [{ id: 1, text: 'old' }], uid: 1 }) } })
    expect(await screen.findByDisplayValue('old', undefined, T)).toBeInTheDocument()
  })
})
