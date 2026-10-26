import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { mockStore } from '@/api/client'
import { focusApi, type ChatItem } from '@/api/focus'
import { renderApp } from '@/test/renderApp'
import { installAndGrant } from '@/test/installAddon'
import { draftFromChat } from './Promote'

const setup = (store: typeof mockStore) => installAndGrant(store, store.workspaces[0].id, 'focus')
const item = (id: string, role: ChatItem['role'], text: string): ChatItem => ({ id, role, kind: 'text', text, ts: null })
const feed = [
  item('1', 'user', 'Make the invite email shorter\nand friendlier'),
  item('2', 'assistant', 'one'), item('3', 'assistant', 'two'), item('4', 'assistant', 'three'), item('5', 'assistant', 'four'),
]
afterEach(() => vi.restoreAllMocks())

it('drafts a ticket from the first ask and the last three assistant texts', () => {
  const draft = draftFromChat(feed)
  expect(draft.title).toBe('Make the invite email shorter')
  expect(draft.sections).toEqual({ requirements: 'Make the invite email shorter\nand friendlier', context: 'two\n\nthree\n\nfour' })
  expect(draftFromChat([])).toEqual({ title: '', sections: {} })
})

it('prefills the create form from the scratch conversation and links the run to the new ticket', async () => {
  const read = vi.spyOn(focusApi, 'transcript').mockResolvedValue({ run: 'scratch1', harness: 'claude-code', available: true, reason: null, items: feed, next: 'o:9', eof: true })
  const link = vi.spyOn(focusApi, 'link').mockResolvedValue({ run: 'scratch1', ticket: 'DEMO-1' })
  const { user, router } = renderApp('/focus?scratch=scratch1', { setup })
  await user.click(await screen.findByRole('button', { name: 'Create ticket from this' }))
  const dialog = await screen.findByRole('dialog', { name: 'New ticket' })
  expect(await within(dialog).findByLabelText('Title')).toHaveValue('Make the invite email shorter')
  expect(read).toHaveBeenCalledWith(mockStore.workspaces[0].id, 'scratch1', expect.objectContaining({ limit: 200 }))
  await user.click(within(dialog).getByRole('button', { name: 'Create' }))
  await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/focus\/DEMO-\d+$/))
  const key = router.state.location.pathname.split('/').at(-1)!
  expect(link).toHaveBeenCalledWith(mockStore.workspaces[0].id, 'scratch1', key)
  expect(mockStore.ticket(key)?.title).toBe('Make the invite email shorter')
})

it('still opens the form, empty, when the transcript cannot be read', async () => {
  vi.spyOn(focusApi, 'transcript').mockRejectedValue(new Error('no'))
  const { user } = renderApp('/focus?scratch=scratch2', { setup })
  await user.click(await screen.findByRole('button', { name: 'Create ticket from this' }))
  const dialog = await screen.findByRole('dialog', { name: 'New ticket' })
  expect(await within(dialog).findByLabelText('Title')).toHaveValue('')
})
