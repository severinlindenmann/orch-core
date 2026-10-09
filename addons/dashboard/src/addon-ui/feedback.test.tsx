import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { api, mockStore } from '@/api/client'
import { ApiError } from '@/api/types'
import { WorkspaceProvider } from '@/app/workspace'
import { AddonNode } from './AddonNode'

afterEach(() => vi.restoreAllMocks())
const T = { timeout: 4000 }

function draw(node: unknown, addon = 'publish') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const ui = (n: unknown) => (
    <QueryClientProvider client={client}>
      <WorkspaceProvider>
        <AddonNode node={n} addon={addon} />
      </WorkspaceProvider>
    </QueryClientProvider>
  )
  const r = render(ui(node))
  return { client, rerender: (n: unknown) => r.rerender(ui(n)) }
}
const table = (rows: { id: string; name: string }[]) => ({
  type: 'table',
  columns: [{ key: 'name', label: 'Name' }],
  rows,
  rowActions: [{ label: 'Stop', action: 'stop', args: { id: '$row.id' } }],
})
const press = async (name: string) => {
  const b = await screen.findByRole('button', { name })
  await waitFor(() => expect(b).toBeEnabled(), T)
  await userEvent.click(b)
}

describe('row feedback follows the row', () => {
  it('an error stays with its row after another row is prepended', async () => {
    vi.spyOn(api, 'runAddonAction').mockRejectedValue(new ApiError(409, { code: 'x', message: 'Sealed to Mara.', retryable: false }))
    const { rerender } = draw(table([{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta' }]))
    const row = () => screen.getByText('Beta').closest('tr')!
    await waitFor(() => expect(within(row()).getByRole('button', { name: 'Stop' })).toBeEnabled(), T)
    await userEvent.click(within(row()).getByRole('button', { name: 'Stop' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Stop app' })) // stopping asks first
    const alert = await screen.findByRole('alert')
    expect(alert.closest('tr')!.previousElementSibling).toBe(row())
    rerender(table([{ id: 'z', name: 'Zed' }, { id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta' }]))
    const after = screen.getByRole('alert')
    expect(after.closest('tr')!.previousElementSibling).toBe(screen.getByText('Beta').closest('tr'))
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })
})

describe('list items with equal titles', () => {
  const list = (ids: string[]) => ({
    type: 'list',
    items: ids.map((id) => ({ id, title: 'One-time link', actions: [{ label: 'Extend', action: 'extend', args: { id } }] })),
  })
  it('an error stays with the pressed item after another with the same title is prepended', async () => {
    vi.spyOn(api, 'runAddonAction').mockRejectedValue(new ApiError(409, { code: 'x', message: 'Gone.', retryable: false }))
    const { rerender } = draw(list(['s1', 's2']))
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Extend' })[1]).toBeEnabled(), T)
    const pressed = screen.getAllByRole('listitem')[1]
    await userEvent.click(within(pressed).getByRole('button', { name: 'Extend' }))
    await within(pressed).findByRole('alert')
    rerender(list(['s0', 's1', 's2']))
    expect(screen.getAllByRole('listitem')[2]).toBe(pressed)
    expect(within(pressed).getByRole('alert')).toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })
})

describe('destructive confirm', () => {
  it('core writes the title; the row\'s name, the manifest label and sentence and the args sit in the From-addon region; the button is the label', async () => {
    draw({ type: 'list', items: [{ title: 'Tariff API notes', actions: [{ label: 'Revoke', action: 'revoke', args: { id: 'x' }, variant: 'danger' }] }] })
    await press('Revoke')
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByRole('heading', { name: 'Confirm: Revoke (revoke) · Publish (publish)' })).toBeInTheDocument()
    const region = within(dialog).getByRole('region', { name: 'From addon publish' })
    expect(region).toHaveTextContent(/Addon says:\s*Tariff API notes/)
    expect(region).toHaveTextContent('Revoke link')
    expect(region).toHaveTextContent('Id (id): x')
    expect(within(dialog).getByRole('button', { name: 'Revoke link' })).toBeInTheDocument()
  })
})

describe('a secret is not kept', () => {
  it('lives in the modal only: no query or mutation cache entry holds it, before or after closing', async () => {
    const SECRET = 'https://p.acme.example/s/TOPSECRET123'
    vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'Made a link.', secret: { label: 'Link', value: SECRET } })
    const { client } = draw({ type: 'button', label: 'Make link', action: 'share_once', variant: 'secondary' })
    await press('Make link')
    await userEvent.click(within(await screen.findByRole('dialog', { name: 'Choose: Share once (share_once) · Publish (publish)' })).getByRole('button', { name: 'Create show-once link' }))
    const dialog = await screen.findByRole('dialog', { name: /Copy this link now/ })
    const held = () => JSON.stringify([client.getMutationCache().getAll().map((m) => m.state), client.getQueryCache().getAll().map((q) => q.state.data)])
    expect(held()).not.toContain('TOPSECRET123')
    await userEvent.click(within(dialog).getByRole('button', { name: 'I saved it' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(held()).not.toContain('TOPSECRET123')
  })
})

describe('a signed action shows everything the host receives, or posts nothing', () => {
  // Publish's `share` made a signed action for these tests (as a new manifest would); restored after each.
  const signShare = () => {
    const pkg = mockStore.addons.find((a) => a.name === 'publish')!
    const before = pkg.actions
    pkg.actions = { ...pkg.actions, share: { minRole: 'member', confirm: 'sign' } }
    return () => (pkg.actions = before)
  }
  it('an arg longer than the dialog shows fails closed: an inline error, no dialog, nothing posted', async () => {
    const restore = signShare()
    const post = vi.spyOn(api, 'runAddonAction')
    draw({ type: 'button', label: 'Share it', action: 'share', args: { note: 'x'.repeat(121) } })
    await press('Share it')
    expect(await screen.findByRole('alert')).toHaveTextContent(/longer than core shows .* nothing was signed or sent/)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(post).not.toHaveBeenCalled()
    restore()
  })
  it('an arg that is not a plain value (a form posting formData) fails closed the same way', async () => {
    const restore = signShare()
    const post = vi.spyOn(api, 'runAddonAction')
    draw({ type: 'form', schema: { type: 'object', properties: { a: { type: 'string', title: 'Note' } } }, action: 'share', submitLabel: 'Share' })
    await press('Share')
    expect(await screen.findByRole('alert')).toHaveTextContent(/a value core cannot show/)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(post).not.toHaveBeenCalled()
    restore()
  })
  it('the confirmation toast: core\'s sentence as the title, the addon\'s message as the labelled description', async () => {
    const restore = signShare()
    const success = vi.spyOn(toast, 'success')
    vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'Approved DEMO-0042 for release.' })
    draw({ type: 'button', label: 'Share it', action: 'share', args: { id: 'x' } })
    await press('Share it')
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Sign and run' }))
    await waitFor(() => expect(success).toHaveBeenCalledWith('Signed: Share (share) · Publish (publish)', expect.objectContaining({ description: 'Addon says: Approved DEMO-0042 for release.' })), T)
    restore()
  })
})
