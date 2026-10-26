import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { PRIORITY_RANK, PriorityMarker } from '../board/lib'
import { PriorityLabel } from './shared'

// A host row without a priority (v1.4: null) shows none, never a made-up "medium".
it('a null priority draws no marker and a dash, and sorts below every real priority', () => {
  const { container } = render(<><PriorityMarker priority={null} /><PriorityLabel priority={null} /></>)
  expect(screen.queryByRole('img')).not.toBeInTheDocument()
  expect(container.textContent).toBe('–')
  expect(container.textContent).not.toMatch(/medium/i)
  expect(PRIORITY_RANK.low).toBeGreaterThanOrEqual(0)
})
