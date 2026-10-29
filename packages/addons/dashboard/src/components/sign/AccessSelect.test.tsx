import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { AccessSelect } from './AccessSelect'

const LENGTHS = [[72, '3 days'], [8, '8 hours']] as const

it('the main view is one sentence; the end time and "every agent" are one click away under Details', () => {
  render(<AccessSelect hours={72} onHours={() => {}} lengths={LENGTHS} prefix="DEMO" agentName="Claude Code" />)
  const box = screen.getByTestId('agent-access')
  expect(box).toHaveTextContent('Agents may work for you for 3 days.')
  const details = screen.getByTestId('agent-access-details') as HTMLDetailsElement
  expect(details.open).toBe(false)
  expect(details.querySelector('summary')).toHaveTextContent('Details')
  expect(screen.getByTestId('agent-access-until')).toHaveTextContent('Access in DEMO ends')
  expect(screen.getByTestId('agent-access-until')).toHaveTextContent('Every agent the runner starts can use it, not only Claude Code.')
})
