import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { TaskStatus } from '@/api/types'
import userEvent from '@testing-library/user-event'
import { HowVerified, ReceiptLine } from './AcceptanceTasks'

const task = (receipt: TaskStatus['receipt']) => ({ id: 'T1', receipt }) as unknown as TaskStatus

describe('ReceiptLine', () => {
  it('labels a receipt an agent attested, and never calls it verified', () => {
    render(<ReceiptLine t={task({ exit: 0, ms: 1234, commit: 'abc1234', attested_by: 'agent' })} />)
    expect(screen.getByText('attested by agent')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/verified/i)
  })

  it('shows a receipt without a label as stored', () => {
    render(<ReceiptLine t={task({ exit: 0, ms: 5 })} />)
    expect(screen.queryByText(/attested by/)).not.toBeInTheDocument()
  })

  it('says "attestation unknown" for a host receipt that carries no label, and never "verified"', () => {
    render(<ReceiptLine t={task({ exit: 0, ms: 5, attested_by: 'unknown' })} />)
    expect(screen.getByText('attestation unknown')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/verified|attested by/i)
  })
})

describe('HowVerified', () => {
  it('shows the verify command as untrusted text: hidden characters are made visible', async () => {
    const t = { id: 'T1', verify: { cmd: 'make test\u202e; rm' }, receipt: null } as unknown as TaskStatus
    render(<HowVerified t={t} />)
    await userEvent.click(screen.getByRole('button', { name: /How it.s verified/ }))
    expect(screen.getByText('make test\\u{202e}; rm')).toBeInTheDocument()
  })
  it('says a task without a command is checked by a person', async () => {
    render(<HowVerified t={{ id: 'T1', verify: null, receipt: null } as unknown as TaskStatus} />)
    await userEvent.click(screen.getByRole('button', { name: /How it.s verified/ }))
    expect(screen.getByText('Checked by a person, no command.')).toBeInTheDocument()
  })
})
