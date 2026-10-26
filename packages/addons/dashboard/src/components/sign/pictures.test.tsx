import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ARM_MS, CLICK_RISK, type ClickViewAnswer } from '@/api/click'
import { forgetPasted, preparePaste } from '@/api/pasteImage'
import { ClickSignPanel } from './ClickSignPanel'

const WS = '705d40abbb8c1c90354a1acaa94c935c'
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3, 4, 5, 6, 7, 8])

async function held() {
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 10, height: 10, close: vi.fn() })))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(((cb: BlobCallback) => cb(new Blob([PNG], { type: 'image/png' }))) as never)
  return preparePaste(new Blob([PNG]))
}

const answer = (images: NonNullable<NonNullable<ClickViewAnswer['view']['content']>['images']>): ClickViewAnswer => ({
  view_digest: 'sha256:' + 'a'.repeat(64),
  view: {
    risk: CLICK_RISK,
    op: 'section.set',
    workspace: { id: WS, name: 'billing', prefix: 'DEMO' },
    fingerprint: 'abcdef012345',
    lines: ['Create DEMO-0001', ''],
    checks: [],
    confirm: { kind: 'click' },
    expires_at: '2026-10-12T12:10:00Z',
    auth: 'convenience',
    device: 'd_x',
    artifacts: 'checked',
    headline: 'Create DEMO-0001',
    content: { headline: 'Create DEMO-0001', images },
  },
})

const mount = (a: ClickViewAnswer) => render(<ClickSignPanel answer={a} problem={null} signing={false} onSign={vi.fn()} onCancel={vi.fn()} />)

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  forgetPasted()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('R3 part B: the sign dialog compares the picture it shows with the digest that is signed', () => {
  it('shows the held picture with its digest and lets Sign turn on when they match', async () => {
    const p = await held()
    mount(answer([{ name: p.name, sha256: p.sha256, bytes: p.bytes.length, alt: 'the broken page' }]))
    expect(screen.getByTestId('picture-ok')).toHaveTextContent('matches what you sign')
    expect(screen.getByAltText('the broken page')).toHaveAttribute('src', p.url)
    act(() => void vi.advanceTimersByTime(ARM_MS + 10))
    expect(screen.getByRole('button', { name: 'Sign' })).toBeEnabled()
  })

  it('shows no picture and keeps Sign off when the signed digest is not the pasted file (a swapped host)', async () => {
    const p = await held()
    mount(answer([{ name: p.name, sha256: `sha256:${'f'.repeat(64)}`, bytes: p.bytes.length, alt: 'the broken page' }]))
    expect(screen.getByTestId('picture-bad')).toHaveTextContent('does not match')
    expect(screen.queryByAltText('the broken page')).toBeNull()
    act(() => void vi.advanceTimersByTime(ARM_MS + 10))
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Sign' }))
    expect(screen.getByRole('button', { name: 'Sign' })).toBeDisabled()
  })

  it('keeps Sign off for a picture this tab never held (the page was reloaded)', () => {
    mount(answer([{ name: 'paste-20261019-101500-deadbeef.png', sha256: `sha256:${'d'.repeat(64)}`, bytes: 10, alt: 'x' }]))
    expect(screen.getByTestId('picture-bad')).toBeInTheDocument()
    act(() => void vi.advanceTimersByTime(ARM_MS + 10))
    expect(screen.getByRole('button', { name: 'Sign' })).toBeDisabled()
  })
})
