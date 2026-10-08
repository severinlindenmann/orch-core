import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderApp } from '@/test/renderApp'
import { WordDiff } from './History'

const T = { timeout: 5000 }

describe('ticket page', () => {
  it('shows DEMO-0043 with tasks T1-T4 and the open question Q2', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    expect(await screen.findByRole('heading', { level: 1, name: 'Load tariff tables as dbt seeds' }, T)).toBeInTheDocument()
    expect(screen.getByTestId('claim-box')).toHaveTextContent(/Claude Code working for Severin/)
    expect(screen.getByTestId('claim-box')).toHaveTextContent(/T2.*sub1.*T3.*sub2/)
    expect(screen.getByTestId('gate-requirements')).toHaveAttribute('data-state', 'approved')

    await user.click(screen.getByRole('tab', { name: /Acceptance & tasks/ }))
    const states = ['T1', 'T2', 'T3', 'T4'].map((id) => document.getElementById(`task-${id}`)?.getAttribute('data-state'))
    expect(states).toEqual(['done', 'doing', 'doing', 'todo'])
    expect(document.getElementById('ac-AC1')).toHaveAttribute('data-state', 'evidenced')
    expect(within(document.getElementById('ac-AC1')!).getByText('agent-asserted')).toBeInTheDocument()
    expect(document.getElementById('ac-AC3')).toHaveAttribute('data-state', 'open')

    await user.click(screen.getByRole('tab', { name: /Questions/ }))
    expect(document.getElementById('question-Q2')).toHaveAttribute('data-state', 'open')
    expect(document.getElementById('question-Q1')).toHaveAttribute('data-state', 'answered')
  })

  it('answers Q2 here after a simulated Touch ID and updates the Questions tab', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(screen.getByRole('tab', { name: /Questions/ }))
    await user.click(await screen.findByRole('radio', { name: /DATE \(local midnight\)/ }))
    await user.click(within(document.getElementById('question-Q2')!).getByRole('button', { name: 'Answer Q2' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/sha256:/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Your answer: DATE/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: /Sign with Touch ID/ }))

    await waitFor(() => expect(document.getElementById('question-Q2')).toHaveAttribute('data-state', 'answered'), T)
    expect(within(document.getElementById('question-Q2')!).getByText(/Severin.*via dashboard.*Touch ID/)).toBeInTheDocument()
  })

  it('tells Tom that DEMO-0044 is not visible to him', async () => {
    renderApp('/ticket/DEMO-0044', { viewer: 'p_tom' })
    expect(await screen.findByText('This ticket is not visible to you', undefined, T)).toBeInTheDocument()
    expect(screen.queryByText('Rotate warehouse service credentials')).not.toBeInTheDocument()
  })

  it('shows an invalidated gate with its reason, and epic children', async () => {
    const view = renderApp('/ticket/DEMO-0046')
    expect(await screen.findByTestId('gate-plan', undefined, T)).toHaveAttribute('data-state', 'invalidated')
    expect(screen.getByTestId('gate-plan')).toHaveTextContent('Plan changed after approval: T2 added')
    view.unmount()

    renderApp('/ticket/DEMO-0040')
    expect(await screen.findByText(/Children \(/, undefined, T)).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: /DEMO-0043/ }, T)).toBeInTheDocument()
  })

  it('renders DEMO-0041 (testing) with a verdict action for the reviewer', async () => {
    const { user } = renderApp('/ticket/DEMO-0041')
    await screen.findByRole('heading', { level: 1, name: /billing reconciliation/ }, T)
    await user.click(screen.getByRole('tab', { name: /Acceptance & tasks/ }))
    expect(within(document.getElementById('ac-AC1')!).getByText('verified by receipt')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Actions/ }))
    expect(await screen.findByRole('menuitem', { name: /Give verdict/ })).toBeInTheDocument()
  })

  it('renders added and removed words in the History changes view', async () => {
    const { user } = renderApp('/ticket/DEMO-0043')
    await screen.findByRole('heading', { level: 1, name: /Load tariff tables/ }, T)
    await user.click(screen.getByRole('tab', { name: 'History' }))
    await user.click(await screen.findByRole('radio', { name: 'Changes' }))
    await user.selectOptions(screen.getByLabelText('Section'), 'requirements')
    const diff = await screen.findByTestId('word-diff')
    expect(diff.querySelectorAll('ins').length).toBeGreaterThan(0)
    expect(diff.querySelectorAll('del').length).toBeGreaterThan(0)
  })
})

describe('WordDiff', () => {
  it('marks inserted and deleted words', () => {
    render(<WordDiff from="load the old tariffs" to="load the new tariffs today" />)
    const diff = screen.getByTestId('word-diff')
    expect(diff.querySelector('del')).toHaveTextContent('old')
    expect(diff.querySelector('ins')?.textContent).toMatch(/new/)
    expect(screen.getByTestId('diff-summary')).toHaveTextContent(/\+2 words.*−1 words/)
  })
})
