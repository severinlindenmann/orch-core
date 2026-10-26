import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Sparkline } from './Sparkline'

describe('Sparkline', () => {
  it('draws extreme finite values without NaN (max - min would overflow)', () => {
    render(<Sparkline values={[-1e308, 1e308, 0]} label="t" />)
    expect(screen.getByRole('img', { name: 't' }).innerHTML).not.toMatch(/NaN|Infinity/)
  })
  it('skips non-finite points and draws nothing with fewer than two', () => {
    const { container } = render(<Sparkline values={[Number.NaN, 3, Infinity]} label="t" />)
    expect(container.innerHTML).toBe('')
  })
  it('a flat series is a flat line', () => {
    render(<Sparkline values={[2, 2, 2]} label="t" />)
    expect(screen.getByRole('img', { name: 't' }).querySelector('polyline')!.getAttribute('points')).toBe('1.0,20.0 36.0,20.0 71.0,20.0')
  })
})
