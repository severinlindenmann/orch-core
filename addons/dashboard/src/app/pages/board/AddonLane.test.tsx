import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
afterEach(() => vi.restoreAllMocks())

/** Makes the github lane carry hostile args on its item action. */
function evilLaneArgs() {
  const real = api.getAddonState.bind(api)
  vi.spyOn(api, 'getAddonState').mockImplementation(async (ws, name) => {
    const st = await real(ws, name)
    if (name !== 'github') return st
    const items = (st.issueItems as { actions: { args: Record<string, unknown> }[] }[]).map((it) => ({ ...it, actions: it.actions.map((a) => ({ ...a, args: { ...a.args, ws: 'evil', ticket: 'X-1' } })) }))
    return { ...st, issueItems: items }
  })
}

async function importClick() {
  const { user } = renderApp('/board', { viewer: 'p_sev' })
  const lane = await screen.findByRole('region', { name: /GitHub issues/ }, T)
  const [button] = await within(lane).findAllByRole('button', { name: 'Import as ticket' }, T)
  await user.click(button)
}

describe('board lane item actions', () => {
  it('never lets lane item args set ws or ticket', async () => {
    evilLaneArgs()
    const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
    await importClick()
    await waitFor(() => expect(post).toHaveBeenCalled(), T)
    const body = post.mock.calls[0][3] as Record<string, unknown>
    expect(body).not.toHaveProperty('ws')
    expect(body).not.toHaveProperty('ticket')
    expect(body).toHaveProperty('id')
  })
  it.each(['javascript:alert(1)', 'http://example.com/x'])('does not open an action result url of %s', async (url) => {
    vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'lane done', url })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    await importClick()
    await screen.findAllByText('lane done', {}, T)
    expect(open).not.toHaveBeenCalled()
  })
  it('opens an https result url', async () => {
    vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'lane done', url: 'https://github.com/a/b/issues/1' })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    await importClick()
    await waitFor(() => expect(open).toHaveBeenCalledWith('https://github.com/a/b/issues/1', '_blank', 'noopener,noreferrer'), T)
  })
})
