import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { AudiencePicker } from './AudiencePicker'

afterEach(() => vi.restoreAllMocks())

function Host({ people }: { people: { user_id: string; name: string }[] }) {
  vi.spyOn(api, 'getHubAudience').mockResolvedValue({ people, teams: [] } as never)
  const [users, setUsers] = useState<string[]>([])
  return <QueryClientProvider client={new QueryClient()}><AudiencePicker ws="w" users={users} groups={[]} withText onUsers={setUsers} onGroups={() => {}} onWithText={() => {}} /></QueryClientProvider>
}

it('the one other person is ticked at the start (r10-P08)', async () => {
  render(<Host people={[{ user_id: 'u1', name: 'Ben Test' }]} />)
  await waitFor(() => expect(screen.getByTestId('hub-rule-person-u1')).toBeChecked())
})

it('with several people nothing is ticked and the line says why nobody sees it', async () => {
  render(<Host people={[{ user_id: 'u1', name: 'Ben' }, { user_id: 'u2', name: 'Ann' }]} />)
  await screen.findByTestId('hub-rule-person-u1')
  expect(screen.getByTestId('hub-rule-person-u1')).not.toBeChecked()
  expect(screen.getByTestId('hub-rule-only-me')).toHaveTextContent('Nobody else sees it until you pick someone.')
})
