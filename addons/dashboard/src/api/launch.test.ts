import { describe, expect, it } from 'vitest'
import { isModelName, renderCommand, shellQuote } from './launch'

describe('model names', () => {
  it('allows aliases and full names', () => {
    for (const m of ['opus', 'sonnet', 'haiku', 'opusplan', 'sonnet[1m]', 'claude-opus-5-5', 'vendor/model:tag', 'gpt-5.1']) expect(isModelName(m), m).toBe(true)
  })
  it('refuses anything a shell or the harness could read as more than a name', () => {
    for (const m of ['sonnet; rm -rf ~', '$(id)', '`id`', '-x', '--dangerously', 'a b', 'son\nnet', '', 'x'.repeat(65), "o'pus", 'a|b', 'a&b', 'a>b'])
      expect(isModelName(m), JSON.stringify(m)).toBe(false)
  })
})

describe('shellQuote and renderCommand', () => {
  it('leaves safe words alone and single-quotes the rest', () => {
    expect(shellQuote('sonnet')).toBe('sonnet')
    expect(shellQuote('DEMO-0044')).toBe('DEMO-0044')
    expect(shellQuote('/orch:work DEMO-0044')).toBe("'/orch:work DEMO-0044'")
    expect(shellQuote('sonnet[1m]')).toBe("'sonnet[1m]'")
    expect(shellQuote('$(id)')).toBe("'$(id)'")
    expect(shellQuote("it's")).toBe("'it'\\''s'")
    expect(shellQuote('')).toBe("''")
  })
  it('renders env then argv, each part quoted', () => {
    expect(renderCommand({ env: { CLAUDE_CODE_SUBAGENT_MODEL: 'haiku' }, argv: ['claude', '--model', 'sonnet[1m]', '/orch:work DEMO-0044'] })).toBe(
      "CLAUDE_CODE_SUBAGENT_MODEL=haiku claude --model 'sonnet[1m]' '/orch:work DEMO-0044'",
    )
  })
})
