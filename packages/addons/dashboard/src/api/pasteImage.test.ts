import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CHUNK, IMAGE_MAX, PasteError, checkPictures, forgetPasted, pastedIn, pastedUrls, preparePaste, sniffImage, uploadPasted, verifiedUrl } from './pasteImage'
import { sha256HexBytes } from './sha256'

const PNG = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
const png = (n = 40) => new Uint8Array([...PNG, ...Array.from({ length: n }, (_, i) => i % 251)])
const text = (s: string) => new TextEncoder().encode(s)

describe('what the first bytes say', () => {
  it('knows PNG, JPEG, WebP, GIF, HEIC and SVG by their bytes and nothing else', () => {
    expect(sniffImage(png())).toBe('png')
    expect(sniffImage(new Uint8Array([0xff, 0xd8, 0xff, 0xe1, 0, 0]))).toBe('jpeg')
    expect(sniffImage(new Uint8Array([...text('RIFF'), 0, 0, 0, 0, ...text('WEBPVP8 ')]))).toBe('webp')
    expect(sniffImage(text('GIF89a....'))).toBe('gif')
    expect(sniffImage(new Uint8Array([0, 0, 0, 0x18, ...text('ftypheic')]))).toBe('heic')
    expect(sniffImage(text('<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'))).toBe('svg')
    expect(sniffImage(text('<?xml version="1.0"?><svg/>'))).toBe('svg')
    expect(sniffImage(text('hello'))).toBeNull()
  })
})

describe('sha256 over bytes', () => {
  it('matches the known digest of "abc"', () => {
    expect(sha256HexBytes(text('abc'))).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
  })
})

/** A canvas that "re-encodes": the blob it hands back is the bytes the test chose for that type and call. */
function stubBrowser(outputs: Array<{ type: 'image/png' | 'image/webp'; bytes: Uint8Array }>, size = { width: 3000, height: 2000 }) {
  const drawn: Array<[number, number]> = []
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ ...size, close: vi.fn() })))
  let call = 0
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (this: HTMLCanvasElement) {
    drawn.push([this.width, this.height])
    return { drawImage: vi.fn() } as unknown as CanvasRenderingContext2D
  } as never)
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation(((cb: BlobCallback, type?: string) => {
    const out = outputs[Math.min(call++, outputs.length - 1)]
    cb(new Blob([out.bytes as BlobPart], { type: out.type === type ? out.type : '' }))
  }) as never)
  return drawn
}

beforeEach(() => forgetPasted())
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('preparePaste', () => {
  it('refuses SVG, GIF, HEIC and unknown bytes before decoding anything', async () => {
    const decode = vi.fn()
    vi.stubGlobal('createImageBitmap', decode)
    for (const [bytes, word] of [
      [text('<svg xmlns="http://www.w3.org/2000/svg"/>'), /SVG/],
      [text('GIF89a....'), /GIF/],
      [new Uint8Array([0, 0, 0, 0x18, ...text('ftypheic')]), /HEIC/],
      [text('plain text'), /not a picture/],
    ] as const) {
      await expect(preparePaste(new Blob([bytes as BlobPart]))).rejects.toThrow(word)
    }
    expect(decode).not.toHaveBeenCalled()
  })

  it('draws the picture again as PNG, names it after its digest and keeps the bytes it made, not the ones it was given', async () => {
    const out = png(120)
    stubBrowser([{ type: 'image/png', bytes: out }], { width: 800, height: 600 })
    const gps = new Uint8Array([0xff, 0xd8, 0xff, 0xe1, ...text('Exif\0\0GPS 47.3N 8.5E'), 0xff, 0xd9])
    const p = await preparePaste(new Blob([gps as BlobPart]), new Date('2026-10-19T10:15:00Z'))
    expect(p.type).toBe('png')
    expect(p.bytes).toEqual(out) // what was re-encoded, never the source (so no EXIF/GPS)
    expect(p.sha256).toBe(`sha256:${sha256HexBytes(out)}`)
    expect(p.name).toBe(`paste-20261019-101500-${sha256HexBytes(out).slice(0, 8)}.png`)
    expect(p.url.startsWith('data:image/png;base64,')).toBe(true)
  })

  it('over 5 MiB it scales the long edge to 2560 px, then tries WebP; over 5 MiB still is refused', async () => {
    const big = new Uint8Array(IMAGE_MAX + 1)
    big.set(PNG)
    const webp = new Uint8Array([...text('RIFF'), 0, 0, 0, 0, ...text('WEBPVP8 '), 1, 2, 3, 4])
    const drawn = stubBrowser([{ type: 'image/png', bytes: big }, { type: 'image/png', bytes: big }, { type: 'image/webp', bytes: webp }])
    const p = await preparePaste(new Blob([png() as BlobPart]))
    expect(p.type).toBe('webp')
    expect(p.name.endsWith('.webp')).toBe(true)
    expect(drawn[0]).toEqual([3000, 2000])
    expect(drawn[1]).toEqual([2560, 1707]) // long edge 2560
    forgetPasted()
    stubBrowser([{ type: 'image/png', bytes: big }, { type: 'image/png', bytes: big }, { type: 'image/webp', bytes: new Uint8Array(IMAGE_MAX + 5) }])
    await expect(preparePaste(new Blob([png() as BlobPart]))).rejects.toThrow(/larger than 5 MiB/)
  })

  it('refuses a picture the browser cannot decode', async () => {
    vi.stubGlobal('createImageBitmap', vi.fn(async () => { throw new Error('bad') }))
    await expect(preparePaste(new Blob([png() as BlobPart]))).rejects.toBeInstanceOf(PasteError)
  })
})

describe('what the text shows', () => {
  it('lists only the pictures this tab holds, from the markdown reference, and names them for the intent', async () => {
    stubBrowser([{ type: 'image/png', bytes: png(30) }])
    const p = await preparePaste(new Blob([png() as BlobPart]))
    const body = `Before\n\n![the broken page](artifact:${p.name})\n\n![other](artifact:paste-20260101-000000-deadbeef.png)\n![remote](https://example.com/x.png)`
    expect(pastedIn([body])).toEqual([{ name: p.name, sha256: p.sha256, bytes: p.bytes.length, alt: 'the broken page' }])
    expect(Object.keys(pastedUrls(body))).toEqual([p.name])
  })

  it('uploads in order, in chunks of at most 512 KiB, the last one flagged', async () => {
    const big = new Uint8Array(CHUNK * 2 + 10)
    big.set(PNG)
    stubBrowser([{ type: 'image/png', bytes: big }])
    const p = await preparePaste(new Blob([png() as BlobPart]))
    const calls: Array<{ offset: number; last: boolean; size: number }> = []
    await uploadPasted(pastedIn([`![x](artifact:${p.name})`]), async (b) => void calls.push({ offset: b.offset, last: b.last, size: b.size }))
    expect(calls.map((c) => c.offset)).toEqual([0, CHUNK, CHUNK * 2])
    expect(calls.map((c) => c.last)).toEqual([false, false, true])
    expect(new Set(calls.map((c) => c.size))).toEqual(new Set([big.length]))
  })
})

describe('the sign dialog check', () => {
  it('is ok for the digest of the held bytes and not for any other, or for a file this tab never had', async () => {
    stubBrowser([{ type: 'image/png', bytes: png(55) }])
    const p = await preparePaste(new Blob([png() as BlobPart]))
    const ref = { name: p.name, sha256: p.sha256, bytes: p.bytes.length, alt: 'x' }
    expect(checkPictures([ref]).map((c) => c.ok)).toEqual([true])
    expect(checkPictures([{ ...ref, sha256: `sha256:${'0'.repeat(64)}` }])[0]).toMatchObject({ ok: false, held: p.sha256 })
    expect(checkPictures([{ ...ref, bytes: ref.bytes + 1 }])[0].ok).toBe(false)
    expect(checkPictures([{ ...ref, name: 'paste-20260101-000000-deadbeef.png' }])[0].ok).toBe(false)
    expect(checkPictures([ref])[0].url).toBe(p.url)
    expect(checkPictures([{ ...ref, sha256: `sha256:${'0'.repeat(64)}` }])[0].url).toBeUndefined()
  })

  it('shows a ticket picture only when the bytes hash to the digest the ticket names', () => {
    const bytes = png(70)
    const b64 = btoa(String.fromCharCode(...bytes))
    const digest = `sha256:${sha256HexBytes(bytes)}`
    expect(verifiedUrl({ type: 'png', b64, digest }, digest)?.startsWith('data:image/png;base64,')).toBe(true)
    expect(verifiedUrl({ type: 'png', b64, digest }, `sha256:${'1'.repeat(64)}`)).toBeUndefined() // another digest in the log
    expect(verifiedUrl({ type: 'webp', b64, digest }, digest)).toBeUndefined() // not the type it says
  })
})
