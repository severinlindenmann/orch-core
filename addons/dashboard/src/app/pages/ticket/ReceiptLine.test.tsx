import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { TaskStatus } from '@/api/types'
import { ReceiptLine } from './AcceptanceTasks'

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
})
