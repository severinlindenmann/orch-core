import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AddonBadge } from './AddonBadge'
import { AddonFrame } from './AddonFrame'

describe('AddonBadge', () => {
  it('renders an "A" with the addon name in its aria-label', () => {
    render(<AddonBadge name="github" />)
    const badge = screen.getByRole('img', { name: 'From addon: github' })
    expect(badge).toHaveTextContent('A')
  })

  it('AddonFrame shows the badge in its header', () => {
    render(
      <AddonFrame addon="usage" title="Usage">
        body
      </AddonFrame>,
    )
    expect(screen.getByRole('img', { name: 'From addon: usage' })).toBeInTheDocument()
    expect(screen.getByText('Usage')).toBeInTheDocument()
  })
})
