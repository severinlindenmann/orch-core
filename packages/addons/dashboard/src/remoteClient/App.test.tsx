import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { ApiError, createApi, launch, type Api, type Row, type State } from './api'

afterEach(cleanup)

const row = (o: Partial<Row>): Row => ({ workspace_id: 'a'.repeat(32), online: true, added: true, name: 'Website', name_from_hub: false, hub_name: 'Website', open: false, ended: null, ...o })
const state = (o: Partial<State> = {}): State => ({
  signed_in: true, hub: { id: 'f'.repeat(32), address: 'hub.acme.example:8443' },
  presence: { available: true, method: 'touch_id', tied: false }, prompt: null, pin: null, open: [], ended: {}, computer: 'Anna Mac', ...o,
})
function fakeApi(o: Partial<Api> = {}): Api {
  return {
    state: vi.fn(async () => state()),
    connect: vi.fn(async () => undefined),
    workspaces: vi.fn(async () => [row({})]),
    enrollStart: vi.fn(async () => ({ id: 'j1', workspace_id: 'a'.repeat(32) })),
    enrollStatus: vi.fn(async () => ({ status: 'words' as const, words: ['maple', 'river', 'tiger', 'cloud', 'seven', 'amber'], message: '', workspace_id: 'a'.repeat(32) })),
    enrollStop: vi.fn(async () => undefined),
    open: vi.fn(async () => undefined),
    end: vi.fn(async () => undefined),
    answer: vi.fn(async () => undefined),
    pin: vi.fn(async () => undefined),
    ...o,
  }
}
const mount = (api: Api | null) => render(<App api={api} pollMs={20} />)

describe('connect', () => {
  it('asks for the link, user, password and code, and says nothing about which one was wrong', async () => {
    const api = fakeApi({ state: vi.fn(async () => state({ signed_in: false })), connect: vi.fn(async () => { throw new ApiError('Check your details and try again.', 'no_session') }) })
    mount(api)
    fireEvent.change(await screen.findByLabelText('Connect link'), { target: { value: 'https://hub.acme.example/c#h1.x' } })
    fireEvent.change(screen.getByLabelText('User'), { target: { value: 'anna' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'secret' } })
    fireEvent.change(screen.getByLabelText('6-digit code'), { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Check your details and try again.')
    expect(api.connect).toHaveBeenCalledWith({ link: 'https://hub.acme.example/c#h1.x', user: 'anna', password: 'secret', code: '123456' })
    expect(screen.getByRole('alert').textContent).not.toMatch(/password|user|code/i)
    expect(screen.getByText('You need to be on the office network for this first step.')).toBeInTheDocument()
  })
  it('shows no progress bar, no cloud icon and never says NetBird', async () => {
    const { container } = mount(fakeApi({ state: vi.fn(async () => state({ signed_in: false })) }))
    await screen.findByText('Connect to your hub')
    expect(container.querySelector('.steps, [role=progressbar]')).toBeNull()
    expect(container.textContent).not.toMatch(/netbird|☁/i)
  })
})

describe('workspaces', () => {
  const rows = [
    row({ workspace_id: '1'.repeat(32), name: 'Website relaunch' }),
    row({ workspace_id: '2'.repeat(32), name: 'Billing', added: false }),
    row({ workspace_id: '3'.repeat(32), name: 'Mobile app', online: false }),
    row({ workspace_id: '4'.repeat(32), name: 'Shop checkout', open: true }),
    row({ workspace_id: '5'.repeat(32), name: 'Hubby', name_from_hub: true }),
  ]
  it('is one line per workspace: Open button, a small End link only while open, Add this computer when not added', async () => {
    mount(fakeApi({ workspaces: vi.fn(async () => rows) }))
    const line = async (name: string) => (await screen.findByText(name)).closest('.row') as HTMLElement
    expect(within(await line('Website relaunch')).getByRole('button', { name: 'Open' })).toBeEnabled()
    expect(within(await line('Website relaunch')).queryByRole('button', { name: 'End' })).toBeNull()
    expect(within(await line('Billing')).getByRole('button', { name: 'Add this computer' })).toBeInTheDocument()
    expect(within(await line('Mobile app')).getByRole('button', { name: 'Open' })).toBeDisabled()
    expect(within(await line('Shop checkout')).getByRole('button', { name: 'End' })).toBeInTheDocument()
    expect(within(await line('Hubby')).getByText('name from the hub')).toBeInTheDocument()
  })
  it('puts an error in the row with the next step', async () => {
    const api = fakeApi({ open: vi.fn(async () => { throw new ApiError('Remote access is off at the workspace.', 'remote.off') }) })
    mount(api)
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Remote access is off at the workspace. Ask the owner to turn it on.')
    expect(alert.closest('.row')).toHaveTextContent('Website')
  })
  it('End asks the client to end the session', async () => {
    const api = fakeApi({ workspaces: vi.fn(async () => [row({ open: true })]) })
    mount(api)
    fireEvent.click(await screen.findByRole('button', { name: 'End' }))
    await waitFor(() => expect(api.end).toHaveBeenCalledWith('a'.repeat(32)))
  })
  it('keeps the technical details behind a fold, with "Tied to this Mac"', async () => {
    mount(fakeApi())
    const d = (await screen.findByText('Technical details')).closest('details')!
    expect(d.open).toBe(false)
    expect(d).toHaveTextContent('Tied to this Mac')
    expect(d).toHaveTextContent('No')
  })
})

describe('add this computer', () => {
  const go = async (api: Api) => {
    mount(api)
    fireEvent.click(await screen.findByRole('button', { name: 'Add this computer' }))
  }
  it('pastes the code in one click and shows six numbered words with a Stop button', async () => {
    Object.assign(navigator, { clipboard: { readText: vi.fn(async () => ' orch-rc1.code ') } })
    const api = fakeApi({ workspaces: vi.fn(async () => [row({ added: false, name: 'Billing' })]) })
    await go(api)
    expect(screen.getByRole('button', { name: '← Workspaces' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Paste code' }))
    expect(await screen.findByRole('heading', { name: 'Read these six words to the owner of Billing' })).toBeInTheDocument()
    expect(api.enrollStart).toHaveBeenCalledWith('orch-rc1.code')
    const words = within(await screen.findByLabelText('Six words')).getAllByRole('listitem')
    expect(words.map((w) => w.textContent)).toEqual(['1maple', '2river', '3tiger', '4cloud', '5seven', '6amber'])
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(api.enrollStop).toHaveBeenCalledWith('j1'))
    expect(await screen.findByRole('button', { name: 'Paste code' })).toBeInTheDocument()
  })
  it('ends with Added', async () => {
    Object.assign(navigator, { clipboard: { readText: vi.fn(async () => 'orch-rc1.code') } })
    const api = fakeApi({ enrollStatus: vi.fn(async () => ({ status: 'added' as const, words: [], message: '', workspace_id: 'a'.repeat(32) })), workspaces: vi.fn(async () => [row({ added: false })]) })
    await go(api)
    fireEvent.click(screen.getByRole('button', { name: 'Paste code' }))
    expect(await screen.findByRole('heading', { name: 'Added' })).toBeInTheDocument()
  })
  it('shows a refused code in plain words, on the same screen', async () => {
    Object.assign(navigator, { clipboard: { readText: vi.fn(async () => 'orch-rb1.x') } })
    const api = fakeApi({ enrollStart: vi.fn(async () => { throw new ApiError('This code is for a browser. Make a code for a computer.', 'remote_client.browser_code') }), workspaces: vi.fn(async () => [row({ added: false })]) })
    await go(api)
    fireEvent.click(screen.getByRole('button', { name: 'Paste code' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('This code is for a browser. Make a code for a computer.')
  })
})

describe('confirm', () => {
  const prompt = { id: 'p1', workspace: 'Billing service', lines: ['Start an agent on ticket AT-12', 'Open a terminal'] }
  it('names the workspace from the session, lists what is asked and says the list is not from the hub', async () => {
    const api = fakeApi({ state: vi.fn(async () => state({ prompt })) })
    mount(api)
    expect(await screen.findByRole('heading', { name: 'Billing service asks you to confirm' })).toBeInTheDocument()
    expect(screen.getByText('Start an agent on ticket AT-12')).toBeInTheDocument()
    expect(screen.getByText('This list comes straight from Billing service, not from the hub.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm with Touch ID' }))
    await waitFor(() => expect(api.answer).toHaveBeenCalledWith('p1', true))
  })
  it('says "with your key" for a security key and Cancel declines', async () => {
    const api = fakeApi({ state: vi.fn(async () => state({ prompt, presence: { available: true, method: 'key', tied: null } })) })
    mount(api)
    expect(await screen.findByRole('button', { name: 'Confirm with your key' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.answer).toHaveBeenCalledWith('p1', false))
  })
})

describe('adding a computer while a confirm is shown', () => {
  it('keeps the job and its words when the confirm screen comes and goes', async () => {
    let prompt: State['prompt'] = null
    const api = fakeApi({ state: vi.fn(async () => state({ prompt })), workspaces: vi.fn(async () => [row({ added: false })]) })
    mount(api)
    fireEvent.click(await screen.findByRole('button', { name: 'Add this computer' }))
    fireEvent.change(screen.getByPlaceholderText('Paste the code here'), { target: { value: 'orch-rc1.x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await screen.findByLabelText('Six words')
    prompt = { id: 'p9', workspace: 'Website', lines: ['Add this computer to workspace abcd1234'] }
    expect(await screen.findByRole('heading', { name: 'Website asks you to confirm' })).toBeInTheDocument()
    prompt = null
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Website asks you to confirm' })).toBeNull())
    expect(screen.getByLabelText('Six words')).toBeVisible() // same job: the words are still there
    expect(api.enrollStart).toHaveBeenCalledTimes(1)
  })
})

describe('security key PIN', () => {
  it('is not asked unless the key asks, and then on its own screen, sent once', async () => {
    const quiet = fakeApi()
    mount(quiet)
    await screen.findByRole('heading', { name: 'Workspaces' })
    expect(screen.queryByLabelText('PIN')).toBeNull()
    cleanup()
    const api = fakeApi({ state: vi.fn(async () => state({ pin: 'q1', presence: { available: true, method: 'key', tied: null } })) })
    mount(api)
    expect(await screen.findByRole('heading', { name: "Enter your security key's PIN" })).toBeInTheDocument()
    expect(screen.getByText('Your key asks for it. It is used once and not saved.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('PIN'), { target: { value: '4821' } })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await waitFor(() => expect(api.pin).toHaveBeenCalledWith('q1', '4821'))
  })
  it('Cancel sends nothing to the key', async () => {
    const api = fakeApi({ state: vi.fn(async () => state({ pin: 'q1' })) })
    mount(api)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(api.pin).toHaveBeenCalledWith('q1', null))
  })
})

describe('the page and its bearer', () => {
  it('tells a hand-opened page to start from orch', async () => {
    mount(null)
    expect(await screen.findByText('Open this page from orch')).toBeInTheDocument()
  })
  it('turns the launch code in the address into a bearer and removes it from the address', async () => {
    history.replaceState(null, '', '/#launch=abc')
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ token: 'tok' }), { status: 200 })) as unknown as typeof fetch
    expect(await launch(fetcher)).toBe('tok')
    expect(location.hash).toBe('')
    expect(JSON.parse((fetcher as unknown as { mock: { calls: [string, RequestInit][] } }).mock.calls[0][1].body as string)).toEqual({ code: 'abc' })
  })
  it('sends its bearer to its own server only, never with cookies', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ workspaces: [] }), { status: 200 }))
    await createApi('tok', fetcher as unknown as typeof fetch).workspaces()
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/remote-client/workspaces')
    expect(init.credentials).toBe('omit')
    expect((init.headers as Record<string, string>).authorization).toBe('Bearer tok')
  })
})
