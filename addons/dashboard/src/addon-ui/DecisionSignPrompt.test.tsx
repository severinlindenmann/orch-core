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
    const dialog = screen.getByRole('dialog', { name: 'Decide for orch core (evil-addon)' })
    expect(within(dialog).getByRole('region').textContent).toContain('From the addon orch core (evil-addon)')
  })
  it('title and covers are core\'s words; the decision\'s title, question, detail and option label sit in the labelled region', () => {
    const qc = new QueryClient()
    qc.setQueryData(['addons'], [{ name: 'evil-addon', title: 'Evil' }])
    render(
      <QueryClientProvider client={qc}>
        <DecisionSignPrompt d={{ ...d, ticket: 'DEMO-0041' }} option={d.options[0]} workspacePrefix="DEMO" onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Decide for Evil (evil-addon)' })
    const covers = within(dialog).getByText('Covers').nextElementSibling!
    expect([...covers.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['Decision x.1', 'Answer: option yes', 'About DEMO-0041', 'In workspace DEMO'])
    for (const addonText of ['Approve the release', 'Ship it?', 'Trust me.']) expect(covers.textContent).not.toContain(addonText)
    const region = within(dialog).getByRole('region', { name: 'From addon evil-addon' })
    expect(region).toHaveTextContent('Title: Approve the release')
    expect(region).toHaveTextContent('Question: Ship it?')
    expect(region).toHaveTextContent('Option yes is labelled: Yes')
    expect(region).toHaveTextContent('Trust me.')
    expect(within(dialog).getByRole('button', { name: 'Send answer' })).toBeInTheDocument()
  })
  it('shows a long question (a permit\'s command) in full, never cut', () => {
    const qc = new QueryClient()
    qc.setQueryData(['addons'], [{ name: 'factory', title: 'AI Factory' }])
    const cmd = `${'a'.repeat(600)} --TAIL`
    render(
      <QueryClientProvider client={qc}>
        <DecisionSignPrompt d={{ ...d, addon: 'factory', question: `DEMO-0050 asks to run: ${cmd}` }} option={d.options[0]} workspacePrefix="DEMO" onSign={() => {}} onClose={() => {}} />
      </QueryClientProvider>,
    )
    const region = within(screen.getByRole('dialog')).getByRole('region', { name: 'From addon factory' })
    expect(region.textContent).toContain(cmd)
    expect(region.textContent).not.toContain('…')
  })
})
