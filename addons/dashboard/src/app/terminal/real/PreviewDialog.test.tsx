import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { TermPreview } from '@/api/runs'
import { EXECUTION_LINE, separateAccount } from './executionLine'
import { PreviewDialog } from './PreviewDialog'

const data = (over: Partial<TermPreview> = {}): TermPreview => ({
  spec: 'f'.repeat(64), display: 'zsh', display_hidden: false, cwd_path: '/work', cwd_path_hidden: false, notice: null,
  as_person: true, login_shell: false, env: [], blocked: null, warnings: [], ...over,
})
const show = (d: TermPreview) => render(<PreviewDialog preview={{ data: d, error: '' }} busy={false} onClose={vi.fn()} onConfirm={vi.fn()} />)

describe('the terminal preview names the account it runs as', () => {
  it('tier B: the plain separate-account line, no "runs as you" words', () => {
    show(data({ as_person: false, execution_line: 'This terminal runs as orch-agent-sev. It cannot read your keys. Never type secrets into it.' }))
    expect(screen.getByTestId('separate-account-line')).toHaveTextContent('separate account (orch-agent-sev)')
    expect(screen.getByTestId('separate-account-line')).toHaveTextContent('cannot read your orch signing keys or passkeys')
    expect(screen.queryByText(EXECUTION_LINE)).toBeNull()
    expect(screen.queryByText('acts as you, not as an agent')).toBeNull()
  })
  it('otherwise the "run as you" line stays', () => {
    show(data())
    expect(screen.getByText(EXECUTION_LINE)).toBeInTheDocument()
    expect(screen.queryByTestId('separate-account-line')).toBeNull()
  })
  it('only an orch-agent user is shown', () => {
    expect(separateAccount('This terminal runs as orch-agent-sev. It cannot read your keys.')).toBe('orch-agent-sev')
    for (const bad of ['This terminal runs as root.', 'This terminal runs as orch-agent-.', 'runs as orch-agent-x', null, undefined, '']) expect(separateAccount(bad)).toBeNull()
  })
})
