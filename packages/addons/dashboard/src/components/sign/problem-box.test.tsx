import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { ClickProblemBox } from './RefusalDevices'

afterEach(cleanup)
const show = (body: string) =>
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ClickProblemBox problem={{ title: 'Not now', body }} ws={undefined} />
    </QueryClientProvider>,
  )

it('a plain sentence wraps at word boundaries in the normal font', () => {
  show('Open it in the dashboard and press the button again.')
  const el = screen.getByText(/Open it in the dashboard/)
  const cls = `${el.className} ${el.parentElement?.className}`
  expect(cls).not.toMatch(/break-all/)
  expect(cls).not.toMatch(/font-mono/)
})

it('a long id inside the sentence can still wrap', () => {
  show(`Ticket ${'x'.repeat(120)} is closed.`)
  expect(screen.getByText(/xxxx/).className).toMatch(/overflow-wrap:anywhere/)
})
