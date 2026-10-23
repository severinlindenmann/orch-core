// R3 part B: a screenshot pasted into a ticket section.
//
// The BROWSER decodes the picture it was given and encodes it again as PNG (WebP when a PNG stays over 5 MiB even after
// scaling). Re-encoding drops EXIF/GPS, colour-profile text and anything appended to the file. The host never decodes
// (it checks headers only), and the signer holds the bytes this module produced, so what the person sees in the
// section, in the sign dialog and what is signed are the same bytes, compared by sha256.
import { sha256HexBytes } from './sha256'

export const IMAGE_MAX = 5 * 1024 * 1024
export const LONG_EDGE = 2560
/** One upload call carries at most this many bytes (base64 stays under the server's 1 MiB body limit). */
export const CHUNK = 512 * 1024
export const MAX_IMAGES = 6

/** The ONE rule for "these bytes are an SVG" (the same as `is_svg` in the dashboard server's reads.py): the first 512 bytes,
 *  trimmed and lower-cased, start with `<svg`, or with `<?xml` / `<!doctype svg` and an `<svg` tag is in the first 4096 bytes. */
export function isSvg(b: Uint8Array): boolean {
  const lead = new TextDecoder().decode(b.slice(0, 512)).trimStart().toLowerCase()
  if (lead.startsWith('<svg')) return true
  return (lead.startsWith('<?xml') || lead.startsWith('<!doctype svg')) && new TextDecoder().decode(b.slice(0, 4096)).toLowerCase().includes('<svg')
}

export type ImageKind = 'png' | 'webp' | 'jpeg' | 'gif' | 'svg' | 'heic'

/** What the first bytes say the file is (a declared type or a file name proves nothing). */
export function sniffImage(b: Uint8Array): ImageKind | null {
  const at = (i: number, s: string) => [...s].every((c, k) => b[i + k] === c.charCodeAt(0))
  if (b[0] === 0x89 && at(1, 'PNG\r\n') && b[6] === 0x1a && b[7] === 0x0a) return 'png'
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'jpeg'
  if (at(0, 'RIFF') && at(8, 'WEBP')) return 'webp'
  if (at(0, 'GIF8')) return 'gif'
  if (at(4, 'ftyp') && /^(heic|heix|hevc|hevx|mif1|msf1|avif)$/.test(String.fromCharCode(...b.slice(8, 12)))) return 'heic'
  if (isSvg(b)) return 'svg'
  return null
}

export class PasteError extends Error {}

export interface Pasted {
  /** `paste-<yyyymmdd>-<hhmmss>-<sha8>.png|webp`: carries the first 8 characters of the digest. */
  name: string
  sha256: string // 'sha256:<hex>'
  bytes: Uint8Array
  type: 'png' | 'webp'
  /** A `data:` URL of exactly `bytes` (the page's CSP allows `data:` images and nothing else). */
  url: string
}

export interface PastedRef {
  name: string
  sha256: string
  bytes: number
  alt: string
}

const store = new Map<string, Pasted>()
/** Pictures pasted in this tab, by name. They are the only bytes the dashboard ever shows for a `paste-…` name. */
export const pasted = (name: string): Pasted | undefined => store.get(name)
export const forgetPasted = () => store.clear()

export function toDataUrl(bytes: Uint8Array, type: 'png' | 'webp'): string {
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return `data:image/${type};base64,${btoa(bin)}`
}

const pad = (n: number, w = 2) => String(n).padStart(w, '0')
export function pasteName(sha: string, at: Date, type: 'png' | 'webp'): string {
  const d = `${at.getUTCFullYear()}${pad(at.getUTCMonth() + 1)}${pad(at.getUTCDate())}`
  const t = `${pad(at.getUTCHours())}${pad(at.getUTCMinutes())}${pad(at.getUTCSeconds())}`
  return `paste-${d}-${t}-${sha.slice(0, 8)}.${type}`
}

const REFUSED: Record<string, string> = {
  svg: 'SVG pictures are not accepted, because they can run code. Paste a screenshot instead.',
  gif: 'GIF pictures are not accepted. Paste a screenshot (PNG, JPEG or WebP).',
  heic: 'HEIC pictures are not accepted. Take a screenshot, or export the picture as PNG or JPEG.',
}

async function encode(source: ImageBitmap, scale: number, type: 'image/png' | 'image/webp', quality?: number): Promise<Blob> {
  const w = Math.max(1, Math.round(source.width * scale))
  const h = Math.max(1, Math.round(source.height * scale))
  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new PasteError('This browser cannot prepare the picture.')
  ctx.drawImage(source, 0, 0, w, h)
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, type, quality))
  if (!blob || blob.type !== type) throw new PasteError('This browser cannot prepare the picture.')
  return blob
}

/**
 * The picture as PNG (or WebP) bytes, decoded and drawn again so nothing but pixels survives. Refuses SVG, GIF, HEIC and
 * anything it cannot decode, and a picture that stays over 5 MiB after scaling its long edge to 2560 px and WebP at 0.9.
 */
export async function preparePaste(file: Blob, now: Date = new Date()): Promise<Pasted> {
  const head = new Uint8Array(await file.slice(0, 600).arrayBuffer())
  const kind = sniffImage(head)
  if (kind === null) throw new PasteError('That is not a picture this page can use. Paste a screenshot (PNG, JPEG or WebP).')
  if (kind === 'svg' || kind === 'gif' || kind === 'heic') throw new PasteError(REFUSED[kind])
  let source: ImageBitmap
  try {
    source = await createImageBitmap(file)
  } catch {
    throw new PasteError('This picture could not be read. Try a screenshot instead.')
  }
  try {
    const scale = Math.min(1, LONG_EDGE / Math.max(source.width, source.height))
    let blob = await encode(source, 1, 'image/png')
    let type: 'png' | 'webp' = 'png'
    if (blob.size > IMAGE_MAX) {
      blob = await encode(source, scale, 'image/png')
      if (blob.size > IMAGE_MAX) {
        blob = await encode(source, scale, 'image/webp', 0.9)
        type = 'webp'
      }
    }
    if (blob.size > IMAGE_MAX) throw new PasteError('The picture is larger than 5 MiB even after shrinking it. Crop it and paste again.')
    const bytes = new Uint8Array(await blob.arrayBuffer())
    const check = sniffImage(bytes)
    if (check !== type) throw new PasteError('This browser could not prepare the picture.')
    const sha = sha256HexBytes(bytes)
    const out: Pasted = { name: pasteName(sha, now, type), sha256: `sha256:${sha}`, bytes, type, url: toDataUrl(bytes, type) }
    store.set(out.name, out)
    return out
  } finally {
    source.close()
  }
}

/** The markdown a paste inserts. */
export const referenceFor = (p: Pasted, alt = 'screenshot') => `![${alt}](artifact:${p.name})`

const REF = /!?\[([^\]\n]{0,120})\]\(artifact:(paste-\d{8}-\d{6}-[0-9a-f]{8}\.(?:png|webp))\)/g

/** The pasted pictures the section texts still show, as the intent describes them. A reference whose bytes this tab does not hold is left out (the host then refuses the unknown file). */
export function pastedIn(texts: Iterable<string>): PastedRef[] {
  const out = new Map<string, PastedRef>()
  for (const text of texts) {
    for (const m of text.matchAll(REF)) {
      const p = store.get(m[2])
      if (!p || out.has(p.name)) continue
      const alt = m[1].trim().replace(/\s+/g, ' ').slice(0, 120) || 'screenshot'
      out.set(p.name, { name: p.name, sha256: p.sha256, bytes: p.bytes.length, alt })
    }
  }
  return [...out.values()].sort((a, b) => (a.name < b.name ? -1 : 1))
}

/** Every pasted picture a text shows is held here, or the reference would be signed without its file. */
export const missingPasted = (texts: Iterable<string>): string[] => {
  const miss = new Set<string>()
  for (const text of texts) for (const m of text.matchAll(REF)) if (!store.has(m[2])) miss.add(m[2])
  return [...miss]
}

/** Uploads the bytes of `images` to the signing helper in order, chunk by chunk (it holds them until the review is signed). */
export async function uploadPasted(
  images: PastedRef[],
  post: (body: { sha256: string; size: number; offset: number; b64: string; last: boolean }) => Promise<unknown>,
): Promise<void> {
  for (const img of images) {
    const p = store.get(img.name)
    if (!p || p.sha256 !== img.sha256) throw new PasteError('A pasted picture is no longer here. Paste it again.')
    for (let off = 0; off < p.bytes.length; off += CHUNK) {
      const part = p.bytes.subarray(off, off + CHUNK)
      let bin = ''
      for (let i = 0; i < part.length; i += 0x8000) bin += String.fromCharCode(...part.subarray(i, i + 0x8000))
      await post({ sha256: p.sha256, size: p.bytes.length, offset: off, b64: btoa(bin), last: off + part.length === p.bytes.length })
    }
  }
}

/**
 * What the sign dialog may show for a picture in the review: the file the browser holds, and only when its digest is
 * the one in the signed event. `ok` false = a mismatch (or a file this tab never had): Sign is disabled.
 */
export type PictureCheck = { name: string; sha256: string; alt: string; ok: boolean; url?: string; held?: string }
export function checkPictures(images: { name: string; sha256: string; bytes: number; alt: string }[] | undefined): PictureCheck[] {
  return (images ?? []).map((i) => {
    const p = store.get(i.name)
    const held = p ? p.sha256 : undefined
    const ok = !!p && p.sha256 === i.sha256 && p.bytes.length === i.bytes && sha256HexBytes(p.bytes) === i.sha256.slice(7)
    return { name: i.name, sha256: i.sha256, alt: i.alt, ok, ...(ok ? { url: p!.url } : {}), ...(held ? { held } : {}) }
  })
}

/** What the server returns for a picture file of a ticket: the bytes, to be hashed here before they are shown. */
export interface ArtifactImage { type: 'png' | 'webp'; b64: string; digest: string }

export function bytesOfB64(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/** A `data:` URL for the bytes of a ticket's picture, only when their sha256 is the digest the ticket names; else `undefined`. */
export function verifiedUrl(img: ArtifactImage, expected: string): string | undefined {
  const bytes = bytesOfB64(img.b64)
  if (sniffImage(bytes) !== img.type || bytes.length > IMAGE_MAX) return undefined
  if (`sha256:${sha256HexBytes(bytes)}` !== expected || img.digest !== expected) return undefined
  return toDataUrl(bytes, img.type)
}

/** The action with its pictures uploaded first (prepare names them by digest only). */
export async function uploadFor(action: { images?: PastedRef[] }, post: Parameters<typeof uploadPasted>[1]): Promise<void> {
  if (action.images?.length) await uploadPasted(action.images, post)
}

/** The pictures pasted in this tab that `text` shows, as `data:` URLs (an editor's preview; never used for a saved ticket). */
export function pastedUrls(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const m of text.matchAll(REF)) {
    const p = store.get(m[2])
    if (p) out[p.name] = p.url
  }
  return out
}
