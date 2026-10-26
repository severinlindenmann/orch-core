// #1069: the picture of an evidence file as a `data:` URL, only when its bytes are what the ticket names.
// The server types and hands over the bytes of a complete, digest-checked read; this re-checks everything on the page:
// the first bytes say the same type, the sha256 of the bytes equals the ticket's digest. It is drawn only in an <img>
// (an image context runs no script and loads nothing, SVG included); SVG is never put in the page as markup.
import type { EvidenceMedia } from './artifactText'
import { bytesOfB64, sniffImage } from './pasteImage'
import { sha256HexBytes } from './sha256'

const MIME: Record<EvidenceMedia['type'], string> = { png: 'image/png', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml' }

export function verifiedMedia(m: EvidenceMedia, expected: string): string | undefined {
  const mime = MIME[m.type]
  if (!mime) return undefined
  const bytes = bytesOfB64(m.b64)
  if (sniffImage(bytes) !== m.type) return undefined
  const want = expected.startsWith('sha256:') ? expected : `sha256:${expected}`
  if (`sha256:${sha256HexBytes(bytes)}` !== want || m.digest !== want) return undefined
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return `data:${mime};base64,${btoa(bin)}`
}
