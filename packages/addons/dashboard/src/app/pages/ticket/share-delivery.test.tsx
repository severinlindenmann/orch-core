import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { setHubScenario } from '@/mocks/hub'
import * as gesture from '@/components/sign/trustedPress'
import { ShareDelivery } from './ShareDelivery'

afterEach(() => { cleanup(); setHubScenario(null); vi.restoreAllMocks() })
it.each([
  ['sent', 'Shared with the hub'],
  ['waiting', 'Waiting for the hub to receive it'],
  ['not_sent', 'Not sent yet'],
  ['refused:too_large', 'This ticket is too large to share through the hub.'],
  ['refused:unknown_user', "Someone you share with can't be reached through the hub."],
  ['refused:other_person', "Someone you share with can't be reached through the hub."],
  ['refused:untrusted prose', 'The hub did not take this share.'],
])('renders %s', (state, text) => {
  const send = vi.fn()
  render(<ShareDelivery state={state} onSend={send} />)
  expect(screen.getByTestId('shared-chip')).toHaveTextContent(text)
  expect(screen.queryByRole('button', { name: 'Send now' }) !== null).toBe(state === 'not_sent')
  expect(send).not.toHaveBeenCalled()
})
it('Send now previews, then only Sign sends the exact plan and updates the badge', async () => {
  setHubScenario('delivery')
  const sync = vi.spyOn(api, 'syncHubShare')
  const preview = vi.spyOn(api, 'getHubPreview')
  const trusted = vi.spyOn(gesture, 'trustedPress').mockReturnValue(false)
  const view = renderApp('/ticket/DEMO-0042')
  await view.user.click(await screen.findByRole('button', { name: 'Send now' }))
  const sign = await screen.findByRole('button', { name: 'Sign' })
  expect(sync).not.toHaveBeenCalled()
  fireEvent.click(sign)
  expect(sync).not.toHaveBeenCalled()
  const plan = (await preview.mock.results.at(-1)!.value).plan
  trusted.mockReturnValue(true)
  fireEvent.click(sign)
  await waitFor(() => expect(sync).toHaveBeenCalledWith(expect.any(String), plan))
  await waitFor(() => expect(screen.getByTestId('shared-chip')).toHaveTextContent('Shared with the hub'))
})
