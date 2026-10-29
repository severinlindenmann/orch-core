// P10: the join dialog folds the workspace line under "Details"; every other review keeps it as before.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { WorkspaceLine } from './WorkspaceLine'
import { storyOf } from './story'

afterEach(cleanup)
const ws = { id: '4f061556aaaaaaaaaaaaaaaaaaaaaaaa', prefix: 'ws-a', name: 'PW' } as never

describe('WorkspaceLine folded', () => {
  it('is under Details (closed) when folded, with the same text', () => {
    render(<WorkspaceLine workspace={ws} many folded />)
    const line = screen.getByTestId('sign-workspace')
    const fold = line.closest('details')!
    expect(fold.open).toBe(false)
    expect(fold.querySelector('summary')!.textContent).toBe('Details')
    expect(line.textContent).toContain('ws-a')
    expect(line.textContent).toContain('aaaaaaaa')
  })
  it('a risk state (always) keeps it in view even when folded', () => {
    render(<WorkspaceLine workspace={ws} many folded always />)
    expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull()
  })
  it('is not folded by default (other dialogs)', () => {
    render(<WorkspaceLine workspace={ws} many />)
    expect(screen.getByTestId('sign-workspace').closest('details')).toBeNull()
  })
  it('only the hub join story folds it', () => {
    const join = storyOf({ action: 'hub_join', hub_card: {}, hub_id: 'h', mode: 'selective' } as never)
    expect(join?.signLabel).toBe('Connect')
    expect(join?.foldWorkspace).toBe(true)
    for (const action of ['hub_leave', 'hub_mode_full'] as const) expect(storyOf({ action } as never)?.foldWorkspace).toBeUndefined()
  })
})
