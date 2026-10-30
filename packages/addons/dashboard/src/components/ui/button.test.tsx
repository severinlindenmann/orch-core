import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Button } from './button'

describe('Button disabled (#421)', () => {
  it('a disabled primary (the Sign button) changes colours, never just opacity', () => {
    render(<Button disabled>Sign</Button>)
    const cls = screen.getByRole('button').className
    expect(cls).toContain('disabled:bg-disabled-bg')
    expect(cls).toContain('disabled:text-disabled-text')
    expect(cls).toContain('disabled:border-disabled-border')
    expect(cls).toContain('disabled:opacity-100')
    expect(cls).not.toContain('disabled:opacity-50')
  })
  it('the destructive and secondary variants get the same set', () => {
    for (const variant of ['destructive', 'secondary', 'outline'] as const) {
      const { unmount } = render(<Button variant={variant} disabled>x</Button>)
      expect(screen.getByRole('button').className).toContain('disabled:bg-disabled-bg')
      unmount()
    }
  })
})
