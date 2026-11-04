import { expect, it } from 'vitest'
import { actorName } from './index'

it('names an agent by its harness when known, else "An agent", never the cli id', () => {
  expect(actorName({ kind: 'agent', id: 'orch-cli', for_name: 'Anna Test' } as never)).toBe('An agent for Anna Test')
  expect(actorName({ kind: 'agent', id: 'claude-code', for_name: 'Anna Test' } as never)).toBe('Claude Code for Anna Test')
  expect(actorName({ kind: 'agent', id: 'orch-cli' } as never)).toBe('An agent')
})
