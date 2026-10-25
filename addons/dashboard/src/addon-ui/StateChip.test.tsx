import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StateBadge, StateChip } from './StateChip'

describe('StateChip', () => {
  it('says On or Off and nothing else', () => {
    const { rerender } = render(<StateChip on />)
    expect(screen.getByText('On')).toHaveAttribute('data-state-chip', 'on')
    rerender(<StateChip on={false} />)
    expect(screen.getByText('Off')).toHaveAttribute('data-state-chip', 'off')
  })
  it('the two other facts are badges, not states', () => {
    render(
      <>
        <StateBadge kind="approval" />
        <StateBadge kind="update" />
      </>,
    )
    expect(screen.getByText('Needs your approval')).not.toHaveAttribute('data-state-chip')
    expect(screen.getByText('Update available')).not.toHaveAttribute('data-state-chip')
  })
})
