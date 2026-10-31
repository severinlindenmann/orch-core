import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { replacedHosts, turnOnSpec } from './TurnOnPublish'

// The turn-on review's lead: the signed server host is always named, the links line stands on its own.
const lead = (where: { server: string; share: string }, row: { suggest_from: 'computer' | 'hub' | null; suggest_hub: string | null }, replaces?: { server: string | null; share: string | null } | null) => {
  render(<>{turnOnSpec(where, row, () => {}, replaces).lead}</>)
}

it('through the hub: the hub’s name is followed by the signed server host, and the links host has its own line', () => {
  lead({ server: 'https://api.acme.example', share: 'https://links.acme.example' }, { suggest_from: 'hub', suggest_hub: 'Acme' })
  expect(screen.getByTestId('where-files-go')).toHaveTextContent('Files go to your hub, Acme (api.acme.example).')
  expect(screen.getByTestId('links-open-at')).toHaveTextContent('Links open at links.acme.example.')
})

it('with no source named the files line still names the signed server host, and the links line is not lost', () => {
  lead({ server: 'https://api.acme.example', share: 'https://links.acme.example' }, { suggest_from: null, suggest_hub: null })
  expect(screen.getByTestId('where-files-go')).toHaveTextContent('Files go to api.acme.example.')
  expect(screen.getByTestId('links-open-at')).toHaveTextContent('Links open at links.acme.example.')
})

it('an empty server gives no destination line, but the links line still shows', () => {
  lead({ server: '', share: 'https://links.acme.example' }, { suggest_from: null, suggest_hub: null })
  expect(screen.getByTestId('links-open-at')).toHaveTextContent('Links open at links.acme.example.')
})

it('replaces: names the old hosts that differ, nothing when the settings are the same', () => {
  const where = { server: 'https://api.acme.example', share: 'https://links.acme.example' }
  expect(replacedHosts(where, { server: 'https://own.example', share: 'https://own-links.example' })).toBe('This replaces own.example and own-links.example.')
  expect(replacedHosts(where, { server: 'https://api.acme.example/', share: 'https://own-links.example' })).toBe('This replaces own-links.example.')
  expect(replacedHosts(where, { server: 'https://api.acme.example', share: 'https://links.acme.example' })).toBeNull()
  expect(replacedHosts(where, { server: null, share: null })).toBe('This replaces your own sharing settings.')
})
