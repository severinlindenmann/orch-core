import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LinkOnce } from './ShareFlow'
import { copyText } from '@/app/copyLink'
vi.mock('@/app/copyLink', () => ({ copyText: vi.fn(async () => true) }))
afterEach(cleanup)

it('P83: Copy is primary, confirms copying, and the complete link wraps without being saved', async () => {
  const link = 'https://share.example.test/' + 'demo-not-a-real-secret-'.repeat(12)
  const done = vi.fn()
  render(<LinkOnce sent={{ share_id: 'demo', ticket: 'DEMO-1', kind: 'secret', link, expires_at: null, view_limit: 0 }} onDone={done} />)
  const field = screen.getByRole('textbox', { name: 'Link' })
  expect(field.tagName).toBe('TEXTAREA')
  expect(field).toHaveValue(link)
  expect(field).toHaveClass('break-all', '[field-sizing:content]')
  expect(screen.getByText('Save this link now: it is shown only once.')).toBeInTheDocument()
  const copy = screen.getByRole('button', { name: 'Copy' })
  expect(copy).toHaveAttribute('data-variant', 'default')
  expect(screen.getByRole('button', { name: /saved the link/ })).toHaveAttribute('data-variant', 'ghost')
  fireEvent.click(copy)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Copied' })).toBeInTheDocument())
  expect(copyText).toHaveBeenCalledWith(link)
  expect(done).not.toHaveBeenCalled()
})
