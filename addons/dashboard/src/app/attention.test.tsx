import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const headerCount = async () => {
  const line = await screen.findByText(/· \d+ need you ·/)
  return Number(/· (\d+) need you/.exec(line.textContent ?? '')![1])
}

describe('one attention count', () => {
  for (const dataset of ['normal', 'busy'] as const) {
    for (const viewer of ['p_sev', 'p_mara'] as const) {
      it(`${dataset}, ${viewer}: the Today link, the Today header and the store agree`, async () => {
        renderApp('/', { viewer, setup: (s) => s.reset(dataset, true) })
        const n = await headerCount()
        const link = await screen.findByRole('link', { name: /Today, \d+ need you/ })
        expect(link).toHaveAccessibleName(`Today, ${n} need you`)
        const ws = mockStore.workspaces[0].id
        expect(n).toBe(mockStore.needsYou(ws).length + mockStore.addonDecisions(ws).length)
        expect(mockStore.workspaceList()[0].needs_you).toBe(n)
      })
    }
  }

  it('the workspace pill shows the same number as the Today link', async () => {
    const { user } = renderApp('/')
    const n = await headerCount()
    await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
    await waitFor(() => expect(screen.getAllByLabelText(`${n} need you`).length).toBeGreaterThan(0))
  })
})
