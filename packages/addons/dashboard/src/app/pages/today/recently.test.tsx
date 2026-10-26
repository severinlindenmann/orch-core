import { render } from '@testing-library/react'
import { expect, it } from 'vitest'
import { Recently } from './side'

it('renders nothing when there is no recent activity', () => {
  const { container } = render(<Recently recent={[]} now="2026-10-26T09:00:00Z" />)
  expect(container).toBeEmptyDOMElement()
})
