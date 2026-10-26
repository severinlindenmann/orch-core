import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { forgetPasted, pastedIn } from '@/api/pasteImage'
import { handoffNewTicket, EMPTY } from './index'
import { Section } from './SectionsEditor'

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 9, 8, 7, 6, 5, 4, 3, 2])

function stubBrowser() {
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 10, height: 10, close: vi.fn() })))
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn() } as unknown as CanvasRenderingContext2D)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(((cb: BlobCallback) => cb(new Blob([PNG], { type: 'image/png' }))) as never)
}

function Harness({ onText }: { onText: (t: string) => void }) {
  const [v, setV] = useState('Steps: open the page.')
  onText(v)
  return <Section type="bug" name="context" value={v} onChange={setV} />
}

const paste = (el: HTMLElement, file: File) => fireEvent.paste(el, { clipboardData: { files: [file], items: [], types: ['Files'] } })

beforeEach(() => forgetPasted())
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('pasting a screenshot into a section', () => {
  it('re-encodes it, inserts the reference at the cursor and shows a thumbnail strip', async () => {
    stubBrowser()
    let text = ''
    render(<Harness onText={(t) => (text = t)} />)
    paste(screen.getByRole('textbox'), new File([new Uint8Array([0xff, 0xd8, 0xff, 0xe1, 1, 2, 3])], 'shot.jpg', { type: 'image/jpeg' }))
    await waitFor(() => expect(text).toMatch(/!\[screenshot\]\(artifact:paste-\d{8}-\d{6}-[0-9a-f]{8}\.png\)/))
    expect(text).toContain('Steps: open the page.')
    expect(await screen.findByTestId('paste-strip')).toBeInTheDocument()
    expect(pastedIn([text])).toHaveLength(1)
  })

  it('refuses an SVG with a plain sentence and leaves the text alone', async () => {
    stubBrowser()
    let text = ''
    render(<Harness onText={(t) => (text = t)} />)
    paste(screen.getByRole('textbox'), new File(['<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'], 'x.svg', { type: 'image/svg+xml' }))
    expect(await screen.findByTestId('paste-note')).toHaveTextContent(/SVG pictures are not accepted/)
    expect(text).toBe('Steps: open the page.')
  })

  it('leaves an ordinary text paste to the browser', () => {
    stubBrowser()
    render(<Harness onText={() => {}} />)
    const notPrevented = fireEvent.paste(screen.getByRole('textbox'), { clipboardData: { files: [], items: [], types: ['text/plain'] } })
    expect(notPrevented).toBe(true)
  })

  it('goes into the signed action as images (with the extra signature counted), and a reference without bytes is a problem', async () => {
    stubBrowser()
    let text = ''
    render(<Harness onText={(t) => (text = t)} />)
    paste(screen.getByRole('textbox'), new File([PNG], 'a.png', { type: 'image/png' }))
    await waitFor(() => expect(pastedIn([text])).toHaveLength(1))
    const made = handoffNewTicket({ ...EMPTY, title: 'T', type: 'bug', sections: { context: text } })
    if ('problem' in made) throw new Error(made.problem)
    expect(made.action).toMatchObject({ action: 'new_sequence', images: [{ alt: 'screenshot', bytes: PNG.length }] })
    forgetPasted()
    expect(handoffNewTicket({ ...EMPTY, title: 'T', type: 'bug', sections: { context: text } })).toMatchObject({ problem: expect.stringMatching(/no longer here/) })
  })
})
