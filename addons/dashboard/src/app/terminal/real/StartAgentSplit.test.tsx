import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StartAgentSplit } from './StartAgentSplit'

afterEach(cleanup)

describe('Start agent split button', () => {
  it('the main part starts at once and carries the mark of the agent last used', async () => {
    const onStart = vi.fn()
    const onChoose = vi.fn()
    render(<StartAgentSplit harness="codex" onStart={onStart} onChoose={onChoose} />)
    expect(document.querySelector('[data-agent-icon="codex"]')).not.toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Start agent' }))
    expect(onStart).toHaveBeenCalledTimes(1)
    expect(onChoose).not.toHaveBeenCalled()
  })
  it('"Change agent or model" is the chevron and opens choose mode', async () => {
    const onStart = vi.fn()
    const onChoose = vi.fn()
    render(<StartAgentSplit harness="claude-code" onStart={onStart} onChoose={onChoose} />)
    await userEvent.click(screen.getByRole('button', { name: 'Change agent or model' }))
    expect(onChoose).toHaveBeenCalledTimes(1)
    expect(onStart).not.toHaveBeenCalled()
  })
  it('disabled: both buttons are off and nothing is called', async () => {
    const onStart = vi.fn()
    const onChoose = vi.fn()
    render(<StartAgentSplit harness="claude-code" onStart={onStart} onChoose={onChoose} disabled />)
    const [main, chevron] = [screen.getByRole('button', { name: 'Start agent' }), screen.getByRole('button', { name: 'Change agent or model' })]
    expect(main).toBeDisabled()
    expect(chevron).toBeDisabled()
    await userEvent.click(main)
    await userEvent.click(chevron)
    expect(onStart).not.toHaveBeenCalled()
    expect(onChoose).not.toHaveBeenCalled()
  })
  it('the chevron carries no agent mark', () => {
    render(<StartAgentSplit harness="codex" onStart={vi.fn()} onChoose={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Change agent or model' }).querySelector('[data-agent-icon]')).toBeNull()
  })
})
