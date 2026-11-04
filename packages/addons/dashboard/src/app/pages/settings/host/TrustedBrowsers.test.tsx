import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({ remote: false, request: vi.fn() }))
vi.mock('@/api/features', async (orig) => ({ ...(await orig<typeof import('@/api/features')>()), showsLogout: () => h.remote }))
vi.mock('@/api/fetchTransport', () => ({ createFetchTransport: () => ({ request: h.request }) }))
import { TrustedBrowsers } from './TrustedBrowsers'

beforeEach(() => { h.remote = false; h.request.mockReset().mockResolvedValue({ status: 200, json: [] }) })
afterEach(() => cleanup())

describe('TrustedBrowsers', () => {
  it('a local session lists the trusted browsers', async () => {
    render(<TrustedBrowsers />)
    expect(await screen.findByTestId('trusted-empty')).toBeTruthy()
    expect(h.request).toHaveBeenCalledWith('GET', '/api/trusted')
  })
  it('a hub or remote session makes no request to /api/trusted and shows nothing', async () => {
    h.remote = true
    const { container } = render(<TrustedBrowsers />)
    await new Promise((r) => setTimeout(r, 20))
    await waitFor(() => expect(container.textContent).toBe(''))
    expect(h.request).not.toHaveBeenCalled()
  })
})
