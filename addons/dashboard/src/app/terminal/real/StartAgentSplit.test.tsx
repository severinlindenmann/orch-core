import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StartAgentSplit } from './StartAgentSplit'

// The dialog itself is tested in StartAgentDialog.test.tsx: here only which mode the two parts open it in.
vi.mock('./StartAgentDialog', () => ({
  lastHarness: () => 'codex',
  StartAgentDialog: ({ ticket, choose }: { ticket: string; choose?: boolean }) => <div data-testid="dialog">{ticket}:{choose ? 'choose' : 'start'}</div>,
}))

afterEach(cleanup)

describe('Start agent split button', () => {
  it('the main part starts at once (no choose mode) and carries the mark of the agent last used', async () => {
    render(<StartAgentSplit ws="w1" ticket="DEMO-1" />)
    expect(document.querySelector('[data-agent-icon="codex"]')).not.toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Start agent' }))
    expect(screen.getByTestId('dialog')).toHaveTextContent('DEMO-1:start')
  })
  it('"Change agent or model" opens the dialog in choose mode', async () => {
    render(<StartAgentSplit ws="w1" ticket="DEMO-1" />)
    await userEvent.click(screen.getByRole('button', { name: 'Change agent or model' }))
    expect(screen.getByTestId('dialog')).toHaveTextContent('DEMO-1:choose')
  })
})
