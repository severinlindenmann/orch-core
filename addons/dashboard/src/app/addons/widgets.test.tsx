import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'

describe('widgets addon page', () => {
  it('lists the templates with their pins and the core types, behind the A badge', async () => {
    renderApp('/addon/widgets/widgets')
    const frame = await waitFor(() => {
      const el = document.querySelector('[data-addon="widgets"]')
      expect(el).toBeTruthy()
      return el as HTMLElement
    }, { timeout: 5000 })
    expect(await within(frame).findByText('before-after@1')).toBeInTheDocument()
    expect(within(frame).getByText('line-chart@1')).toBeInTheDocument()
    expect(within(frame).getByText('option-prototype@1')).toBeInTheDocument()
    expect(within(frame).getAllByText(/^[0-9a-f]{12}/).length).toBeGreaterThanOrEqual(3)
    expect(within(frame).getByText('kv')).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: /From addon: widgets/ }).length).toBeGreaterThan(0)
  })
})
