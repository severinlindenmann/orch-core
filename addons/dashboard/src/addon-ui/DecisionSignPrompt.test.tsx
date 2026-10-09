import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { AddonDecision } from '@/api/types'
import { DecisionSignPrompt } from './DecisionSignPrompt'

const d: AddonDecision = { kind: 'decision', id: 'x.1', addon: 'evil-addon', title: 'Approve the release', question: 'Ship it?', detail: 'Trust me.', options: [{ key: 'yes', label: 'Yes', primary: true }], action: 'decide' } as AddonDecision

describe('DecisionSignPrompt', () => {
  it('names the addon by its title and always its package id, even when it calls itself "orch core"', () => {
    const qc = new QueryClient()
    qc.setQueryData(['addons'], [{ name: 'evil-addon', title: 'orch core' }])
    render(
      <QueryClientProvider client={qc}>
        <DecisionSignPrompt d={d} option={d.options[0]} workspacePrefix="DEMO" onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('Covers').nextElementSibling!.textContent).toContain('Requested by the addon orch core (evil-addon)')
    expect(within(dialog).getByRole('region').textContent).toContain('From the addon orch core (evil-addon)')
  })
})
