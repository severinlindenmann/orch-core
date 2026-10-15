import { describe, expect, it } from 'vitest'
import type { Workspace } from '@/api/types'
import { displayName } from './shared'

const ws = { members: [{ person: 'p_sev', name: 'Severin' }] } as unknown as Workspace

describe('displayName', () => {
  it('shows … while the workspace members load, not the raw id', () => {
    expect(displayName({ workspace: undefined, agents: [] }, 'p_sev')).toBe('…')
  })
  it('shows the name once the members are there, and the id for someone unknown', () => {
    expect(displayName({ workspace: ws, agents: [] }, 'p_sev')).toBe('Severin')
    expect(displayName({ workspace: ws, agents: [] }, 'p_ghost')).toBe('p_ghost')
  })
})
