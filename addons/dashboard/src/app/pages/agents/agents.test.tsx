import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockStore } from '@/api/client'
import { renderApp } from '@/test/renderApp'
import { groupOf } from '@/app/attention'

describe('Agents page', () => {
  it('groups sessions by state: waiting on you first, stopped collapsed', async () => {
    renderApp('/agents')
    expect(await screen.findByRole('region', { name: /Waiting on you/ })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: /Working/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Stopped/ })).toHaveAttribute('aria-expanded', 'false')
  })
  it('a row is one line with a subagents toggle, and shows no raw ids or models at first level', async () => {
    const { user } = renderApp('/agents')
    const waiting = await screen.findByRole('region', { name: /Waiting on you/ })
    const toggle = within(waiting).getByRole('button', { name: /\+\d+ subagents?/ })
    expect(screen.queryByText(/^s_[a-z0-9]+(\.\d+)?$/)).toBeNull()
    expect(screen.queryByText(/claude-|opus|sonnet|gpt-/i)).toBeNull()
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(within(waiting).getByText(/DEMO-0043\/T3/)).toBeInTheDocument()
  })
  it('the ticket count opens claims, model and session id', async () => {
    const { user } = renderApp('/agents')
    const working = await screen.findByRole('region', { name: /Working/ })
    await user.click(within(working).getAllByRole('button', { name: /\d+ tickets?/ })[0])
    expect(within(working).getByText(/^s_[a-z0-9]+$/)).toBeInTheDocument()
  })
  it('summarises the workspace in the header with the same session count as Today', async () => {
    renderApp('/agents')
    expect(await screen.findByRole('heading', { level: 1, name: 'Agents' })).toBeInTheDocument()
    const line = await screen.findByText(/agent sessions? · \d+ waiting on you/)
    expect(line).toHaveTextContent(/ · grant until 18:00$|your grant until 18:00$/)
    const n = Number(/(\d+) agent session/.exec(line.textContent ?? '')![1])
    expect(n).toBe(mockStore.agents(mockStore.workspaces[0].id).filter((s) => !s.parent).length)
  })
  it('Today counts agents working from the same root sessions (not waiting on you, not stopped)', async () => {
    renderApp('/')
    const line = await screen.findByText(/· \d+ need you ·/)
    const n = Number(/(\d+) agents? working/.exec(line.textContent ?? '')![1])
    const all = mockStore.agents(mockStore.workspaces[0].id)
    expect(n).toBe(all.filter((s) => !s.parent && groupOf(s, all, mockStore.viewer) === 'working').length)
  })
  it('revokes a grant after signing, and its sessions stop', async () => {
    const { user } = renderApp('/agents')
    const row = await screen.findByRole('row', { name: /gr_01J9Z8/ })
    const stopped = () => Number(/Stopped \((\d+)\)/.exec(screen.queryByRole('button', { name: /Stopped/ })?.textContent ?? '(0)')?.[1] ?? 0)
    const before = stopped()
    await user.click(within(row).getByRole('button', { name: 'Revoke' }))
    await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
    expect(await within(screen.getByRole('row', { name: /gr_01J9Z8/ })).findByText('revoked')).toBeInTheDocument()
    await waitFor(() => expect(stopped()).toBeGreaterThan(before))
  })
  it('stops the main session when its grant is revoked', async () => {
    const { user } = renderApp('/agents')
    const waiting = await screen.findByRole('region', { name: /Waiting on you/ })
    expect(within(waiting).getByRole('listitem', { name: /Claude Code for Severin, waiting/ })).toBeInTheDocument()
    await user.click(within(await screen.findByRole('row', { name: /gr_01J9Z8/ })).getByRole('button', { name: 'Revoke' }))
    await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
    await within(screen.getByRole('row', { name: /gr_01J9Z8/ })).findByText('revoked')
    await waitFor(() => expect(screen.queryByRole('listitem', { name: /Claude Code for Severin, waiting/ })).toBeNull())
    await user.click(screen.getByRole('button', { name: /Stopped/ }))
    expect(within(screen.getByRole('region', { name: /Stopped/ })).getAllByRole('listitem', { name: /Claude Code for Severin, stopped/ }).length).toBeGreaterThan(0)
  })
  it('lists recent refusals in plain words, with no codes, and links to all activity', async () => {
    renderApp('/agents')
    const region = await screen.findByRole('region', { name: 'Recent refusals' })
    expect(within(region).getByText(/tried to approve a plan\. Only people can approve\./)).toBeInTheDocument()
    expect(within(region).getAllByRole('listitem').length).toBeLessThanOrEqual(5)
    expect(within(region).queryByText(/human_only|claim\.held|lease\.held/)).toBeNull()
    expect(within(region).queryByText('stopped after 3 refusals')).toBeNull()
  })
  it('the viewer cannot revoke or issue', async () => {
    renderApp('/agents', { viewer: 'p_tom' })
    await screen.findByRole('region', { name: /Working/ })
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Issue grant…' })).toBeNull()
  })
  it('a member cannot issue or revoke grants either (owners and maintainers only)', async () => {
    renderApp('/agents', {
      viewer: 'p_tom',
      setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'member.role_changed', person: 'p_tom', role: 'member', from: 'viewer' }),
    })
    await screen.findByRole('region', { name: /Working/ })
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Issue grant…' })).toBeNull()
  })
  it('issues a grant after signing', async () => {
    const { user } = renderApp('/agents')
    await user.click(await screen.findByRole('button', { name: 'Issue grant…' }))
    await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
    await waitFor(() => expect(screen.getAllByRole('row', { name: /gr_/ })).toHaveLength(5))
  })
})
