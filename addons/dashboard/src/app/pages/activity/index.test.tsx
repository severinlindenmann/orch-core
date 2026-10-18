import { expect, it } from 'vitest'
import { actorName } from './index'

it('names an agent by its harness when known, else "An agent", never the cli id', () => {
  expect(actorName({ kind: 'agent', id: 'orch-cli', for_name: 'Severin' } as never)).toBe('An agent for Severin')
  expect(actorName({ kind: 'agent', id: 'claude-code', for_name: 'Severin' } as never)).toBe('Claude Code for Severin')
  expect(actorName({ kind: 'agent', id: 'orch-cli' } as never)).toBe('An agent')
})
