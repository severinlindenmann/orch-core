// M3 H-5: the owner's Company tab. Skill text is drawn as plain text; Turn on sends the digest that was shown; a tool
// switch sends its pin; a server that needs a separate agent user has no Turn on; a changed item is said plainly.
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import type { OrgSecrets, OrgShown } from '@/api/org'
import { resetMockOrg } from '@/mocks/org'
import { renderApp } from '@/test/renderApp'

const T = { timeout: 4000 }
beforeAll(async () => { await import('@/app/pages/settings') }, 60000)
beforeEach(() => resetMockOrg())
afterEach(() => { cleanup(); vi.restoreAllMocks() })

const page = () => renderApp('/w/DEMO/settings/company')
const openRow = async (name: string) => {
  fireEvent.click(await screen.findByTestId(`company-open-${name}`, {}, T))
  return await screen.findByTestId('company-sheet', {}, T)
}

describe('the Company tab', () => {
  it('lists skills and tools with a status word and one button each', async () => {
    page()
    expect(await screen.findByText('Company skills', {}, T)).toBeTruthy()
    expect(screen.getByText('Company tools')).toBeTruthy()
    expect(screen.getByTestId('company-status-style-guide').textContent).toBe('Off')
    expect(screen.getByTestId('company-status-support-tone').textContent).toBe('Changed — look again')
    expect(screen.getByTestId('company-on-style-guide').textContent).toBe('Review and turn on')
    expect(screen.getByTestId('company-sync').textContent).toBe('Get the latest from your hub')
  })

  it('explains company secrets and marks the tools that use one', async () => {
    page()
    await screen.findByText('Company tools', {}, T)
    expect(screen.getByTestId('company-intro').textContent).toMatch(/company secret.*You never see it.*separate agent user/)
    expect(screen.getByTestId('company-secret-billing').textContent).toBe('Uses a company secret from your hub')
    expect(screen.queryByTestId('company-secret-crm')).toBeNull()
    expect(screen.queryByTestId('company-secret-style-guide')).toBeNull()
    expect(document.body.textContent).not.toMatch(/hub_secret/)
  })

  it('lists the company secrets with status, expiry and users, and never a value', async () => {
    page()
    expect(await screen.findByText('Company secrets', {}, T)).toBeTruthy()
    expect(await screen.findByTestId('company-secret-row-billing-key', {}, T)).toBeTruthy()
    expect(screen.getByTestId('company-secret-status-billing-key').textContent).toBe('Ready')
    expect(screen.getByTestId('company-secret-until-billing-key').textContent).toMatch(/^Until \d{1,2} \w{3}$/)
    expect(screen.getByTestId('company-secret-usedby-billing-key').textContent).toBe('Used by billing')
    expect(screen.getByTestId('company-secret-status-crm-token').textContent).toBe('Expired')
    expect(screen.getByTestId('company-secret-status-old-key').textContent).toBe('Not shared any more')
    expect(screen.queryByTestId('company-secret-until-old-key')).toBeNull()
  })

  it('says plainly when no company secret is shared', async () => {
    const reply: OrgSecrets & { secrets: unknown[] } = { available: true, secrets: [] }
    vi.spyOn(api, 'getOrgSecrets').mockResolvedValue(reply)
    page()
    expect((await screen.findByTestId('company-secrets-empty', {}, T)).textContent).toBe('No company secrets are shared with this workspace.')
  })

  it('shows no secrets panel on a host without the op', async () => {
    vi.spyOn(api, 'getOrgSecrets').mockResolvedValue({ available: false, secrets: [] })
    page()
    await screen.findByText('Company tools', {}, T)
    expect(screen.queryByText('Company secrets')).toBeNull()
  })

  it('shows skill text as plain text: markdown and HTML stay literal', async () => {
    const hostile = 'Use **bold** and <img src=x onerror=alert(1)> and [link](http://x)'
    const shown: OrgShown = { kind: 'skill', status: 'off', notes: [], untrusted: true, needs_isolation: null,
      item: { kind: 'skill', name: 'style-guide', digest: 'sha256:' + 'a'.repeat(64), text: hostile, text_hidden: false } }
    vi.spyOn(api, 'getOrgItem').mockResolvedValue(shown)
    page()
    const sheet = await openRow('style-guide')
    const pre = await within(sheet).findByTestId('company-skill-text', {}, T)
    expect(pre.textContent).toBe(hostile)
    expect(pre.querySelector('img, a, strong, b')).toBeNull()
    expect(pre.children.length).toBe(0)
  })

  it('says the honest limits of a skill before Turn on', async () => {
    page()
    const sheet = await openRow('style-guide')
    const limits = (await within(sheet).findByTestId('company-limits', {}, T)).textContent ?? ''
    expect(limits).toMatch(/orch's rules/)
    expect(limits).toMatch(/already read|stays read/)
  })

  it('Turn on sends the digest of the shown item, then closes', async () => {
    const accept = vi.spyOn(api, 'acceptOrgItem')
    page()
    const sheet = await openRow('style-guide')
    await within(sheet).findByTestId('company-skill-text', {}, T)
    fireEvent.click(within(sheet).getByTestId('company-accept'))
    await waitFor(() => expect(accept).toHaveBeenCalledWith(expect.any(String), 'skill', 'style-guide', 'sha256:' + 'a'.repeat(64)))
    await waitFor(() => expect(screen.queryByTestId('company-sheet')).toBeNull(), T)
    expect((await screen.findByTestId('company-status-style-guide', {}, T)).textContent).toBe('On')
    expect(screen.getByTestId('company-off-style-guide').textContent).toBe('Turn off')
  })

  it('Turn off declines', async () => {
    page()
    fireEvent.click(await screen.findByTestId('company-on-style-guide', {}, T))
    fireEvent.click(await screen.findByTestId('company-accept', {}, T))
    const off = await screen.findByTestId('company-off-style-guide', {}, T)
    const decline = vi.spyOn(api, 'declineOrgItem')
    fireEvent.click(off)
    await waitFor(() => expect(decline).toHaveBeenCalledWith(expect.any(String), 'skill', 'style-guide'))
    await waitFor(() => expect(screen.getByTestId('company-status-style-guide').textContent).toBe('Off'), T)
  })

  it('a changed item says so when the host answers org: changed, and shows the new version', async () => {
    const accept = vi.spyOn(api, 'acceptOrgItem').mockRejectedValue(new ApiError(400, { code: 'invalid.input', message: 'org: changed: x is not the version you were shown', retryable: false }))
    page()
    const sheet = await openRow('style-guide')
    await within(sheet).findByTestId('company-skill-text', {}, T)
    fireEvent.click(within(sheet).getByTestId('company-accept'))
    await waitFor(() => expect(accept).toHaveBeenCalled())
    expect((await within(sheet).findByTestId('company-message', {}, T)).textContent).toMatch(/changed while you were looking/)
    expect(screen.getByTestId('company-sheet')).toBeTruthy() // still open, nothing was turned on
  })

  it('a server shows its address, sign-in, description and tools; a tool switch sends its pin', async () => {
    page()
    fireEvent.click(await screen.findByTestId('company-on-crm', {}, T))
    const sheet = await screen.findByTestId('company-sheet', {}, T)
    expect((await within(sheet).findByTestId('company-url', {}, T)).textContent).toBe('https://crm.example.com/mcp')
    expect(within(sheet).getByTestId('company-auth').textContent).toBe('No sign-in')
    expect(within(sheet).getByText('Look up customers and open deals.')).toBeTruthy()
    expect(within(sheet).getByTestId('company-limits').textContent).toMatch(/only hides it from the agent|Off only hides/)
    expect(within(sheet).getByTestId('company-limits').textContent).toMatch(/not check|untrusted/i)
    fireEvent.click(within(sheet).getByTestId('company-accept'))
    await waitFor(() => expect(screen.queryByTestId('company-sheet')).toBeNull(), T)
    fireEvent.click(screen.getByTestId('company-open-crm'))
    const again = await screen.findByTestId('company-sheet', {}, T)
    const tool = vi.spyOn(api, 'setOrgTool')
    fireEvent.click(await within(again).findByTestId('company-tool-switch-find_customer', {}, T))
    await waitFor(() => expect(tool).toHaveBeenCalledWith(expect.any(String), 'crm', 'find_customer', true, 'pin-find-1'))
    await waitFor(() => expect(within(again).getByTestId('company-tool-switch-find_customer').getAttribute('aria-checked')).toBe('true'), T)
    fireEvent.click(within(again).getByTestId('company-tool-switch-find_customer'))
    await waitFor(() => expect(tool).toHaveBeenLastCalledWith(expect.any(String), 'crm', 'find_customer', false, undefined))
  })

  it('a changed tool says so and stays off', async () => {
    const shown: OrgShown = { kind: 'mcp', status: 'on', notes: [], untrusted: true, needs_isolation: null,
      item: { kind: 'mcp', name: 'crm', digest: 'sha256:' + 'c'.repeat(64), url: 'https://crm.example.com/mcp', auth: 'none', secret: null, description: null,
        tools: [{ name: 'find_customer', description: null, pin: 'p2', status: 'changed' }] } }
    vi.spyOn(api, 'getOrgItem').mockResolvedValue(shown)
    page()
    const sheet = await openRow('crm')
    expect((await within(sheet).findByTestId('company-tool-changed-find_customer', {}, T)).textContent).toBe('Changed — turn on again to allow')
    expect(within(sheet).getByTestId('company-tool-switch-find_customer').getAttribute('aria-checked')).toBe('false')
  })

  it('a server that needs a separate agent user has no Turn on and says why', async () => {
    page()
    fireEvent.click(await screen.findByTestId('company-on-billing', {}, T))
    const sheet = await screen.findByTestId('company-sheet', {}, T)
    expect((await within(sheet).findByTestId('company-auth', {}, T)).textContent).toBe('Company secret: billing-key')
    expect(within(sheet).getByTestId('company-needs-isolation')).toBeTruthy()
    expect(within(sheet).queryByTestId('company-accept')).toBeNull()
    expect(within(sheet).getByTestId('company-needs-isolation').textContent).toMatch(/separate agent user/)
    expect(within(sheet).getByTestId('company-limits').textContent).not.toMatch(/separate agent user/) // said once
  })

  it('Get the latest asks the hub, and says plainly when the hub is out of reach', async () => {
    const sync = vi.spyOn(api, 'syncOrg').mockRejectedValue(new ApiError(502, { code: 'hub_unreachable', message: 'x', retryable: true }))
    page()
    fireEvent.click(await screen.findByTestId('company-sync', {}, T))
    await waitFor(() => expect(sync).toHaveBeenCalledWith(expect.any(String)))
    expect((await screen.findByTestId('company-note', {}, T)).textContent).toMatch(/Showing what was saved/)
    expect(screen.getByTestId('company-open-style-guide')).toBeTruthy() // the saved list stays
  })

  it('shows each tool in full: title, whole description, input schema as plain text; control characters are removed', async () => {
    const long = 'x'.repeat(1500)
    const shown: OrgShown = { kind: 'mcp', status: 'on', notes: [], untrusted: true, needs_isolation: null,
      item: { kind: 'mcp', name: 'crm', digest: 'sha256:' + 'c'.repeat(64), url: 'https://crm.example.com/mcp', auth: 'none', secret: null, description: null,
        tools: [
          { name: 'find_customer', title: 'Find a customer', description: 'Finds.\x1b[2J ' + long, input_schema: '{\n  "type": "<b>object</b>"\x1b[2J\n}', pin: 'p1', status: 'off' },
          { name: 'bulk_export', description: null, pin: '', status: 'too_long' },
        ] } }
    vi.spyOn(api, 'getOrgItem').mockResolvedValue(shown)
    page()
    const sheet = await openRow('crm')
    expect((await within(sheet).findByTestId('company-tool-title-find_customer', {}, T)).textContent).toBe('Find a customer')
    const desc = within(sheet).getByTestId('company-tool-desc-find_customer').textContent ?? ''
    expect(desc).toBe('Finds.[2J ' + long) // ESC removed, nothing truncated
    const schema = within(sheet).getByTestId('company-tool-schema-find_customer')
    expect(schema.textContent).toBe('{\n  "type": "<b>object</b>"[2J\n}')
    expect(schema.querySelector('b')).toBeNull()
    expect(within(sheet).getByTestId('company-tool-toolong-bulk_export').textContent).toBe('Too long to review — stays off')
    expect(within(sheet).queryByTestId('company-tool-switch-bulk_export')).toBeNull()
  })
})

describe('a server with details that cannot be shown', () => {
  it('has no Turn on, says why, and counts the tools that could not be listed', async () => {
    const shown: OrgShown = { kind: 'mcp', status: 'off', notes: [], untrusted: true, needs_isolation: null,
      item: { kind: 'mcp', name: 'crm', digest: 'sha256:' + 'c'.repeat(64), url: null, auth: 'none', secret: null, description: null, tools: [], fields_hidden: true, hidden_tools: 2 } }
    vi.spyOn(api, 'getOrgItem').mockResolvedValue(shown)
    page()
    const sheet = await openRow('crm')
    expect((await within(sheet).findByTestId('company-fields-hidden', {}, T)).textContent).toBe('Some details cannot be shown, so it cannot be turned on.')
    expect(within(sheet).getByTestId('company-hidden-tools').textContent).toBe('2 tools could not be shown (they stay off).')
    expect(within(sheet).queryByTestId('company-accept')).toBeNull()
  })
})

describe('plain()', () => {
  it('drops C0/C1 controls but keeps newline', async () => {
    const { plain } = await import('@/api/org')
    expect(plain('a\x1b[2Jb\nc\x07\x85d\x7f')).toBe('a[2Jb\ncd')
    expect(plain(null)).toBe('')
    expect(plain('a\u202eb\u200bc\u2028d')).toBe('abcd') // bidi, zero-width, line separator
  })
})
