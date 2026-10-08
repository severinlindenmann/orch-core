import { describe, expect, it } from 'vitest'
import { parseNode } from './nodes'

const button = (action: string) => parseNode({ type: 'button', label: 'Go', action })

describe('action ids', () => {
  it.each(['share', 'save_settings', 'a.b-c', '9lives', '_private'])('accepts %s', (id) => {
    expect(button(id).ok).toBe(true)
  })
  it.each(['..', '.', '.hidden', '-flag', '', 'a/b', 'x'.repeat(65)])('rejects %j (must start with a letter, digit or underscore)', (id) => {
    expect(button(id).ok).toBe(false)
  })
  it('rejects a bad id in list item actions too', () => {
    expect(parseNode({ type: 'list', items: [{ title: 'x', actions: [{ label: 'Up', action: '..' }] }] }).ok).toBe(false)
  })
})
