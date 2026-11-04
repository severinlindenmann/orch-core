import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import type { HostSettings } from '@/api/settings'
import type { Workspace } from '@/api/types'
import { ReposSection } from './Repos'

vi.mock('./useViewerRole', () => ({ useViewerRole: () => 'owner' }))
vi.mock('./useSettingsHandoff', () => ({ useSettingsHandoff: () => ({ ask: () => {}, dialog: null, inline: false }) }))

afterEach(cleanup)

test('r11-P15: a repository row shows its name and a status word; folder, main branch and remote sit under Details', () => {
  const data = {
    available: true,
    repos: [
      { name: 'ws-a', path: '/Users/anna/projects/ws-a', default_branch: null, remote: 'git@example.org:a/ws-a.git', check: { state: 'present', branch: 'main' } },
    ],
  } as unknown as HostSettings
  render(<ReposSection workspace={{ id: 'w1' } as Workspace} data={data} onSettled={() => {}} />)
  const row = screen.getByRole('list', { name: 'Repositories' }).querySelector('li')!
  const details = within(row).getByTestId('repo-details-ws-a')
  expect(details).not.toHaveAttribute('open')
  for (const raw of ['/Users/anna/projects/ws-a', 'No main branch set', 'git@example.org:a/ws-a.git']) {
    const at = within(row).getByText(raw, { exact: false })
    expect(at.closest('details')).toBe(details) // only inside the fold
  }
  expect(row.textContent).not.toContain('no main branch set')
  expect(within(row).getByText('ws-a')).toBeTruthy()
  expect(within(row).getByTestId('repo-check-ws-a')).toHaveTextContent(/^Ready$/)
  expect(within(row).getByText('In place, on branch main').closest('details')).toBe(details)
})
