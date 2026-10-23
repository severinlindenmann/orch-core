// The feedback page's data: numbered items with optional sub-items, pasted screenshots and the report built from them.
// Pure functions only (the page keeps the state), so the numbering, report and ZIP are testable without a browser.

export interface FeedbackItem {
  id: number
  text: string
  /** pasted screenshots as data URLs */
  shots: string[]
  kids: FeedbackItem[]
}
export interface FeedbackState {
  items: FeedbackItem[]
  uid: number
  /** UTC time the round started, e.g. 20261023T164900Z; keeps one day's feedback apart from the next */
  round: string | null
}

export const emptyState = (): FeedbackState => ({ items: [], uid: 0, round: null })
export const newItem = (state: FeedbackState): FeedbackItem => ({ id: ++state.uid, text: '', shots: [], kids: [] })

/** A UTC stamp without separators: 2026-10-23T16:49:00.123Z -> 20261023T164900Z. */
export const stamp = (d: Date = new Date()): string => d.toISOString().replace(/[-:]/g, '').replace(/\.\d+/, '')

export const isFilled = (it: FeedbackItem): boolean => it.text.trim() !== '' || it.shots.length > 0 || it.kids.some(isFilled)
export const hasOwnContent = (it: FeedbackItem): boolean => it.text.trim() !== '' || it.shots.length > 0
export const flatten = (items: FeedbackItem[]): FeedbackItem[] => items.flatMap((it) => [it, ...flatten(it.kids)])

/** Keeps exactly one empty field at the end of the list (the next feedback) and starts or ends the round with the content. */
export function normalize(state: FeedbackState, now: Date = new Date()): FeedbackState {
  const items = state.items
  if (items.length === 0 || isFilled(items[items.length - 1])) items.push(newItem(state))
  while (items.length > 1 && !isFilled(items[items.length - 1]) && !isFilled(items[items.length - 2])) items.pop()
  // A sub-list that has content gets its own empty tail too (1.1 filled -> 1.2 opens).
  const tails = (list: FeedbackItem[]) => {
    for (const it of list) {
      if (it.kids.length > 0) {
        if (isFilled(it.kids[it.kids.length - 1])) it.kids.push(newItem(state))
        while (it.kids.length > 1 && !isFilled(it.kids[it.kids.length - 1]) && !isFilled(it.kids[it.kids.length - 2])) it.kids.pop()
      }
    }
  }
  tails(items)
  const any = items.some(isFilled)
  state.round = any ? (state.round ?? stamp(now)) : null
  return state
}

export interface Numbered {
  path: string
  item: FeedbackItem
}
/** Items with content in order, numbered 1, 1.1, 1.2, 2 ... (empty ones are skipped and do not take a number). */
export function numbered(items: FeedbackItem[], prefix = ''): Numbered[] {
  const out: Numbered[] = []
  let n = 0
  for (const item of items.filter(isFilled)) {
    n += 1
    const path = prefix ? `${prefix}.${n}` : `${n}`
    out.push({ path, item }, ...numbered(item.kids, path))
  }
  return out
}

/** `20261023T164900Z-fb1.1-img1.png`: the round keeps two rounds from overwriting each other in one folder. */
export const imageName = (round: string, path: string, k: number): string => `${round}-fb${path}-img${k + 1}.png`

/** The markdown report. `dir` is where the images are (`images/` inside the ZIP, or the folder the person saved them to). */
export function buildReport(state: FeedbackState, dir: string, zip: boolean): string {
  const rows = numbered(state.items)
  if (rows.length === 0 || !state.round) return ''
  const round = state.round
  let s = `# Feedback report ${round} (${rows.length} items)\n\nRound: ${round} (UTC). This is one feedback round; ignore other rounds.\n\n`
  s += 'Work through these in order. Feedback 1.1 is a sub-point of Feedback 1. Open each referenced image file with your file/image reader; the screenshot shows what the text describes.\n\n'
  if (!zip) s += `Images are in: ${dir}\n\n`
  for (const { path, item } of rows) {
    const ind = '  '.repeat(path.split('.').length - 1)
    s += `${ind}## Feedback ${path}\n`
    const body = item.text.trim().split('\n').map((l) => ind + l).join('\n')
    if (body.trim()) s += `${body}\n`
    item.shots.forEach((_, k) => {
      const name = imageName(round, path, k)
      s += `${ind}![${name}](${dir}${name})\n`
    })
    s += '\n'
  }
  return `${s.trim()}\n`
}

export function dataUrlBytes(url: string): Uint8Array<ArrayBuffer> {
  const bin = atob(url.slice(url.indexOf(',') + 1))
  return Uint8Array.from(bin, (c) => c.charCodeAt(0))
}

const CRC = (() => {
  const t: number[] = []
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c >>> 0
  }
  return t
})()
const crc32 = (u: Uint8Array<ArrayBuffer>): number => {
  let c = 0xffffffff
  for (const b of u) c = CRC[(c ^ b) & 255] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}
const u16 = (v: number) => [v & 255, (v >> 8) & 255]
const u32 = (v: number) => [v & 255, (v >> 8) & 255, (v >> 16) & 255, (v >>> 24) & 255]

/** A ZIP with the files stored uncompressed (screenshots are PNG already), so no library is needed. */
export function makeZip(files: { name: string; data: Uint8Array<ArrayBuffer> }[]): Blob {
  const enc = new TextEncoder()
  const parts: BlobPart[] = []
  const central: Uint8Array<ArrayBuffer>[] = []
  let offset = 0
  for (const f of files) {
    const name = enc.encode(f.name)
    const crc = crc32(f.data)
    const size = f.data.length
    const local = new Uint8Array([0x50, 0x4b, 3, 4, ...u16(20), ...u16(0x800), ...u16(0), ...u16(0), ...u16(0x21), ...u32(crc), ...u32(size), ...u32(size), ...u16(name.length), ...u16(0)])
    parts.push(local, name, f.data)
    central.push(new Uint8Array([0x50, 0x4b, 1, 2, ...u16(20), ...u16(20), ...u16(0x800), ...u16(0), ...u16(0), ...u16(0x21), ...u32(crc), ...u32(size), ...u32(size), ...u16(name.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0), ...u32(0), ...u32(offset)]), name)
    offset += local.length + name.length + size
  }
  const centralSize = central.reduce((a, x) => a + x.length, 0)
  parts.push(...central, new Uint8Array([0x50, 0x4b, 5, 6, 0, 0, 0, 0, ...u16(files.length), ...u16(files.length), ...u32(centralSize), ...u32(offset), 0, 0]))
  return new Blob(parts, { type: 'application/zip' })
}

/** FEEDBACK.md plus images/<name> for every screenshot. */
export function zipFiles(state: FeedbackState): { name: string; data: Uint8Array<ArrayBuffer> }[] {
  const round = state.round ?? ''
  const files = [{ name: 'FEEDBACK.md', data: new TextEncoder().encode(buildReport(state, 'images/', true)) }]
  for (const { path, item } of numbered(state.items)) item.shots.forEach((src, k) => files.push({ name: `images/${imageName(round, path, k)}`, data: dataUrlBytes(src) }))
  return files
}
