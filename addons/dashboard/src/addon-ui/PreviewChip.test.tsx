import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PreviewChip } from './PreviewChip'

const draw = (preview: boolean | undefined) => {
  const client = new QueryClient()
  client.setQueryData(['addons'], [{ name: 'x', title: 'X', preview }, { name: 'y', title: 'Y' }])
  return render(
    <QueryClientProvider client={client}>
      <PreviewChip name="x" />
      <PreviewChip name="y" />
    </QueryClientProvider>,
  )
}

describe('PreviewChip', () => {
  it('is core-rendered from the package field: shown only for a package marked preview', () => {
    draw(true)
    expect(screen.getAllByText('Preview')).toHaveLength(1)
  })
  it('shows nothing when the package is not a preview', () => {
    draw(undefined)
    expect(screen.queryByText('Preview')).not.toBeInTheDocument()
  })
  it('is not orange (orange is reserved for the A badge)', () => {
    draw(true)
    expect(screen.getByText('Preview').className).not.toMatch(/addon/)
  })
})
