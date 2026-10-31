import { cleanup, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { setHubScenario } from '@/mocks/hub'
import { renderApp } from '@/test/renderApp'

afterEach(() => { cleanup(); setHubScenario(null); vi.restoreAllMocks() })
it.each(['/w/DEMO/settings/hub'])('offers unsent shares on %s without sending', async path => {
  setHubScenario('delivery')
  const sync = vi.spyOn(api, 'syncHubShare')
  const view = renderApp(path)
  expect(await screen.findByText('1 shares were made before you joined and are not sent yet.')).toBeTruthy()
  await view.user.click(screen.getByRole('button', { name: 'Send now' }))
  expect(await screen.findByRole('button', { name: 'Sign' })).toBeTruthy()
  expect(sync).not.toHaveBeenCalled()
})
