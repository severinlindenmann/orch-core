import { act, cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ReverseUndoHost, requestReverse } from './ReverseUndoHost'

const mocks = vi.hoisted(() => ({ sign: vi.fn(), runner: vi.fn() }))
vi.mock('./SignPopover', () => ({ SignPopover: (props: unknown) => { mocks.sign(props); return null } }))
vi.mock('./SignShell', () => ({ SignShell: ({ children }: { children: unknown }) => children }))
vi.mock('@/components/RunnerSwitch', () => ({ RunnerSwitch: (props: unknown) => { mocks.runner(props); return null } }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

it('add-on Undo opens a fresh turn-on review with capabilities, never the old digest', () => {
  render(<ReverseUndoHost />)
  act(() => requestReverse({ ws: 'w1', action: { action: 'addon_grant', name: 'github' } }))
  const props = mocks.sign.mock.lastCall![0]
  expect(props.action).toEqual({ action: 'addon_grant', name: 'github' })
  expect(props).not.toHaveProperty('view_digest')
  expect(props.ws).toBe('w1')
})

it('runner Undo opens the normal fresh authority review', () => {
  render(<ReverseUndoHost />)
  act(() => requestReverse({ ws: 'w1', runner: true }))
  expect(mocks.runner.mock.lastCall![0]).toMatchObject({ ws: 'w1', on: true, initiallyOpen: true })
})
