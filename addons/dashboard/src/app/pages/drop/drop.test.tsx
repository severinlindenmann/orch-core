// Workspace → Drop (proposed drop/1, fixtures only): the gate, the size check before any upload, real progress and cancel,
// each error text, untrusted names rendered as text, download only as an attachment, the version cap, the delete
// confirmation and the disabled downloads of pruned, expired, deleted and unclaimed entries.
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setFeatures } from '@/api/features'
import { CHUNK_BYTES, DropError, dropApi, dropError, downloadName, dropServed, fmtBytes, MAX_FILE_NAME, realDropApi, saveBytes, sizeRefusal, uploadBlob } from '@/api/drop'
import { DROP_SAMPLE_ERRORS, dropPreviewForTests, resetLayoutPreview } from '@/dev/layoutPreview'
import { createDropFixture, dropItem, installDropFixture, PHONE, PHONE_SHARE } from '@/dev/dropFixture'
import { renderApp } from '@/test/renderApp'
import { fmtDateTime } from '@/lib/time'

const T = { timeout: 4000 }
const MiB = 1024 * 1024

let created: Blob[] = []
let clicked: HTMLAnchorElement[] = []
beforeEach(() => {
  created = []
  clicked = []
  // jsdom has no object URLs and no navigation: record what a download would do.
  URL.createObjectURL = vi.fn((b: Blob) => (created.push(b), `blob:demo/${created.length}`)) as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clicked.push(this.cloneNode() as HTMLAnchorElement)
  })
})
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  resetLayoutPreview()
  setFeatures(null)
})

const open = (scenario: string, opts?: Parameters<typeof dropPreviewForTests.set>[0]) => {
  dropPreviewForTests.set(opts ?? null)
  return renderApp(`/__layout/${scenario}`)
}
const fixture = () => dropPreviewForTests.fixture()!
const ops = (op: string) => fixture().calls.filter((c) => c.op === op)
const rowOf = async (name: string) => (await screen.findAllByTestId('drop-row', {}, T)).find((r) => r.textContent?.includes(name))!
const file = (bytes: number, name = 'notes.txt', type = 'text/plain') => new File([new Uint8Array(bytes).fill(7)], name, { type })
const pick = async (view: ReturnType<typeof renderApp>, f: File) => view.user.upload(await screen.findByTestId('drop-file-input', {}, T), f)

/** Tests that push a real multi-MiB file through the chunked upload in jsdom: pure CPU, slow on a busy CI runner. */
const BIG_FILE_MS = 30_000

describe('the gate', () => {
  it('the real api answers "not available" and the gate needs both the feature and a served api', async () => {
    expect(realDropApi.served('w')).toBe(false)
    await expect(realDropApi.list('w', { direction: 'received' })).rejects.toMatchObject({ code: 'drop.not_served' })
    await expect(realDropApi.blobPut('w', { offset: 0, b64: '', last: true })).rejects.toMatchObject({ code: 'drop.not_served' })
    setFeatures({ features: [] })
    expect(dropServed('w')).toBe(false)
    setFeatures({ features: ['drop'] })
    expect(dropServed('w')).toBe(false) // the real implementation: not until the ops are agreed
    const undo = installDropFixture(createDropFixture())
    expect(dropServed('w')).toBe(true)
    setFeatures({ features: [] })
    expect(dropServed('w')).toBe(false)
    undo()
    expect(dropApi.served).toBe(realDropApi.served)
  })

  it('drop-unavailable says so and calls nothing', async () => {
    open('drop-unavailable')
    expect(await screen.findByTestId('drop-unavailable', {}, T)).toHaveTextContent('Drop is not available on this host yet.')
    expect(screen.queryByTestId('drop-zone')).toBeNull()
    expect(fixture().calls).toEqual([])
  })
})

describe('the list', () => {
  it('shows received items with kind, size, author, expiry and state; tabs switch to Sent', async () => {
    const view = open('drop-list')
    const row = await rowOf('quarterly-report.pdf')
    expect(within(row).getByTestId('drop-kind')).toHaveTextContent('File · application/pdf')
    expect(within(row).getByTestId('drop-size').textContent).toMatch(/KiB$/)
    expect(within(row).getByTestId('drop-author')).toHaveTextContent('Severin · device d_phone_1 · from a phone')
    expect(within(row).getByTestId('drop-state')).toHaveTextContent('Live')
    expect(within(row).getByTestId('drop-download')).toBeEnabled()
    // Agent-sealed is marked.
    expect(within(await rowOf('build-log.txt')).getByTestId('drop-agent')).toHaveTextContent('sealed by an agent')
    await view.user.click(screen.getByTestId('drop-tab-sent'))
    await rowOf('handover.pdf')
    expect(ops('list').map((c) => c.args[0])).toContain('sent')
  })

  it('pruned, expired and deleted entries disable the download with a reason; unclaimed is explained', async () => {
    open('drop-list')
    const cases: [string, string, RegExp][] = [
      ['old-export.zip', 'Content pruned', /pruned: only the newest 8 versions/],
      ['expired-photo.heic', 'Expired', /expired: its content is gone/],
      ['deleted-contract.pdf', 'Deleted', /deleted for everyone/],
      ['scan-from-phone.jpg', 'Live', /Not claimed yet: until this workspace’s host claims it/],
    ]
    for (const [name, state, reason] of cases) {
      const row = await rowOf(name)
      expect(within(row).getByTestId('drop-state')).toHaveTextContent(state)
      expect(within(row).getByTestId('drop-download')).toBeDisabled()
      expect(within(row).getByTestId('drop-download-reason').textContent).toMatch(reason)
    }
    expect(within(await rowOf('scan-from-phone.jpg')).getByTestId('drop-unclaimed')).toBeInTheDocument()
    // No claim button (the carriage isn't decided).
    expect(screen.queryByRole('button', { name: /claim/i })).toBeNull()
    // Tombstones offer no Delete.
    expect(within(await rowOf('expired-photo.heic')).queryByTestId('drop-delete')).toBeNull()
  })

  it('pages with the cursor and updates live from a drop sub item', async () => {
    const view = open('drop-list')
    await rowOf('quarterly-report.pdf')
    expect(screen.getAllByTestId('drop-row')).toHaveLength(8)
    await view.user.click(screen.getByTestId('drop-next'))
    await waitFor(() => expect(screen.getAllByTestId('drop-row')).toHaveLength(3), T)
    expect(ops('list').at(-1)!.args).toEqual(['received', '8'])
    await view.user.click(screen.getByTestId('drop-prev'))
    await rowOf('quarterly-report.pdf')
    act(() => fixture().emit('received', dropItem({ name: 'just-arrived.png', author: PHONE })))
    await rowOf('just-arrived.png')
  })
})

describe('untrusted names', () => {
  it('render as text: no element from a name, bidi and control characters shown, a hidden name never guessed', async () => {
    open('drop-long-names')
    const names = await screen.findAllByTestId('drop-name', {}, T)
    expect(names.map((n) => n.textContent)).toContain('<img src=x onerror=alert(1)>.html')
    expect(document.querySelector('img[src="x"]')).toBeNull()
    expect(document.querySelectorAll('[data-testid="drop-name"] *:not(bdi)')).toHaveLength(0)
    expect(names.map((n) => n.textContent)).toContain('invoice\\u{202e}fdp.exe')
    expect(names.map((n) => n.textContent)).toContain('line one\\u{a}line two\\u{0}.txt')
    for (const n of names) expect(n.textContent).not.toMatch(/[‮\u0000]/)
    expect(screen.getByTestId('drop-name-hidden')).toHaveTextContent("a name that can't be shown")
  })

  it('the uploaded file is sent under its own name, shown as text', async () => {
    const view = open('drop-list')
    await pick(view, file(10, 'a‮b<i>.txt'))
    await screen.findByTestId('drop-done', {}, T)
    expect(ops('send')[0].args[0]).toMatchObject({ name: 'a‮b<i>.txt', mime: 'text/plain' })
    expect(screen.getByTestId('drop-done').textContent).toBe('Sent a\\u{202e}b<i>.txt.')
  })
})

describe('upload', () => {
  it('refuses a file above the limit BEFORE any upload (no blob.put), naming the limit from the host', async () => {
    const view = open('drop-list', { info: { max_bytes: 1000 } })
    expect(await screen.findByTestId('drop-limit', {}, T)).toHaveTextContent('1000 bytes')
    await pick(view, file(1001, 'big.bin'))
    const refused = await screen.findByTestId('drop-refused', {}, T)
    expect(refused).toHaveTextContent('This file is 1001 bytes; Drop takes at most 1000 bytes per file. Nothing was uploaded.')
    expect(refused).toHaveTextContent('drop.too_large')
    expect(ops('blob.put')).toHaveLength(0)
    expect(ops('send')).toHaveLength(0)
    expect(sizeRefusal(25 * MiB, { max_bytes: 25 * MiB })).toBeNull()
    expect(sizeRefusal(25 * MiB + 1, { max_bytes: 25 * MiB })).toMatch(/at most 25 MiB/)
  })

  it('shows real progress in bytes and percent, then "Sealing and sending…", then done; the host gets the exact bytes', async () => {
    const view = open('drop-list', { chunkDelayMs: 40, sendDelayMs: 60 })
    const f = file(2 * MiB, 'figures.xlsx', 'application/vnd.ms-excel')
    await pick(view, f)
    const seen = new Set<string>()
    await waitFor(() => {
      const t = screen.queryByTestId('drop-progress-text')?.textContent
      if (t) seen.add(t)
      expect(screen.queryByTestId('drop-sealing')).not.toBeNull()
    }, T)
    expect(seen).toContain(`${fmtBytes(CHUNK_BYTES)} of 2 MiB (37 %)`)
    expect(screen.getByTestId('drop-sealing')).toHaveTextContent('Sealing and sending figures.xlsx…')
    await screen.findByTestId('drop-done', {}, T)
    const puts = ops('blob.put').map((c) => c.args[0] as { offset: number; last: boolean; has_blob: boolean; size?: number })
    expect(puts.map((p) => p.offset)).toEqual([0, CHUNK_BYTES, 2 * CHUNK_BYTES])
    expect(puts.map((p) => p.last)).toEqual([false, false, true])
    expect(puts[0]).toMatchObject({ has_blob: false, size: 2 * MiB })
    const sent = ops('send')[0].args[0] as { blob: string }
    expect(fixture().blobs.get(sent.blob)).toEqual(new Uint8Array(2 * MiB).fill(7))
    // The list shows the new item under Sent.
    await view.user.click(screen.getByTestId('drop-tab-sent'))
    await rowOf('figures.xlsx')
  }, BIG_FILE_MS)

  it('Cancel during the browser upload stops it, drops the host blob and sends nothing', async () => {
    const view = open('drop-list', { stallAfterChunks: 1 })
    await pick(view, file(2 * MiB, 'slow.bin'))
    await waitFor(() => expect(screen.getByTestId('drop-progress-text')).toHaveTextContent(`${fmtBytes(CHUNK_BYTES)} of 2 MiB`), T)
    await view.user.click(screen.getByTestId('drop-cancel'))
    expect(await screen.findByTestId('drop-cancelled', {}, T)).toHaveTextContent('Upload of slow.bin cancelled. Nothing was sent.')
    await waitFor(() => expect(ops('blob.drop')).toHaveLength(1))
    expect(ops('send')).toHaveLength(0)
  }, BIG_FILE_MS)

  it('drag and drop uploads the dropped file', async () => {
    open('drop-list')
    const zone = await screen.findByTestId('drop-zone', {}, T)
    fireEvent.drop(zone, { dataTransfer: { files: [file(5, 'dragged.txt')] } })
    await screen.findByTestId('drop-done', {}, T)
    expect(ops('send')[0].args[0]).toMatchObject({ name: 'dragged.txt' })
  })

  it('a host refusal at send time is explained with its code (quota)', async () => {
    const view = open('drop-errors')
    await pick(view, file(5, 'x.txt'))
    const err = await within(screen.getByTestId('drop-zone')).findByTestId('drop-error', {}, T)
    expect(err).toHaveAttribute('data-code', 'drop.quota')
    expect(err).toHaveTextContent("A Drop limit is full: this workspace's Drop storage (511 MiB of 512 MiB used). Sending won't work again until something is removed or expires.")
  })
})

describe('error texts', () => {
  const ctx = { max_bytes: 25 * MiB, version_cap: 1024 }
  const text = (i: number) => dropError(DROP_SAMPLE_ERRORS[i], ctx)
  it('each code has a clear sentence', () => {
    expect(text(0)).toEqual({ code: 'drop.too_large', text: 'This file is too large for Drop: the limit is 25 MiB per file.' })
    expect(text(1).text).toBe("A Drop limit is full: this workspace's Drop storage (511 MiB of 512 MiB used). Sending won't work again until something is removed or expires.")
    expect(text(2).text).toBe("A Drop limit is full: this device's documents in the workspace (64 documents of 64 documents used). Sending won't work again until something is removed or expires.")
    expect(text(3)).toMatchObject({ code: 'drop.quota', quota: expect.stringMatching(/^drop_new_U+$/) })
    expect(text(3).text).toMatch(/^A Drop limit is full: a quota the page does not know \(1 byte of 1 byte used\)\. Sending won't work again/)
    expect(text(4).text).toBe('This document already has 1024 versions, the most a document keeps. No new version can be saved.')
    expect(text(5).text).toBe('Someone saved a newer version; reload.')
    expect(text(6).text).toBe('This item has expired: its content is gone. The entry stays listed for a while.')
    expect(text(7).text).toMatch(/^This version was pruned/)
    expect(text(8).text).toBe("Drop can't reach the relay right now (it is unreachable, or this host is not linked to one).")
    expect(text(9).text).toMatch(/^You can't see this item/)
    expect(text(10)).toEqual({ code: 'drop.overloaded', text: 'Drop is busy right now. Try again in 5 s.' })
  })

  it('the drop-errors scenario shows every text with its code', async () => {
    open('drop-errors')
    const gallery = await screen.findByTestId('drop-error-gallery', {}, T)
    const codes = within(gallery).getAllByTestId('drop-error').map((e) => e.getAttribute('data-code'))
    for (const c of ['drop.too_large', 'drop.quota', 'drop.version_cap', 'drop.version_conflict', 'drop.expired', 'drop.pruned', 'drop.unavailable', 'drop.not_visible', 'drop.overloaded'])
      expect(codes).toContain(c)
    // The unknown quota name is untrusted text.
    expect(gallery.textContent).toContain('Quota: drop_new_U')
  })

  it('a download refused by the host shows its text in the row', async () => {
    const view = open('drop-list', { readError: { '*': new DropError(410, { code: 'drop.pruned', message: 'drop.pruned', retryable: false }) } })
    const row = await rowOf('quarterly-report.pdf')
    await view.user.click(within(row).getByTestId('drop-download'))
    expect(await within(row).findByTestId('drop-error', {}, T)).toHaveAttribute('data-code', 'drop.pruned')
    expect(clicked).toHaveLength(0)
  })
})

describe('download', () => {
  it('is an attachment only: a blob URL of type octet-stream behind an anchor with download; never inline, never a new tab', async () => {
    const opened = vi.spyOn(window, 'open').mockImplementation(() => null)
    const view = open('drop-list')
    const row = await rowOf('quarterly-report.pdf')
    await view.user.click(within(row).getByTestId('drop-download'))
    await waitFor(() => expect(clicked).toHaveLength(1), T)
    const a = clicked[0]
    expect(a.getAttribute('download')).toBe('quarterly-report.pdf')
    expect(a.getAttribute('href')).toMatch(/^blob:/)
    expect(a.getAttribute('target')).toBeNull()
    expect(created).toHaveLength(1)
    expect(created[0].type).toBe('application/octet-stream')
    expect(opened).not.toHaveBeenCalled()
    // The (HTML) content is never put in the page.
    expect(document.querySelector('iframe, object, embed, img[src^="blob:"], video, audio')).toBeNull()
    expect(document.body.innerHTML).not.toContain('alert(1)</script>')
    expect(document.querySelector('a[download]')).toBeNull() // the anchor is removed again
    expect(ops('read')[0].args).toEqual([expect.stringMatching(/^demo-obj-/), 1])
  })

  it('a hidden name downloads under a fixed name', async () => {
    const view = open('drop-long-names')
    const row = (await screen.findAllByTestId('drop-row', {}, T)).find((r) => within(r).queryByTestId('drop-name-hidden'))!
    await view.user.click(within(row).getByTestId('drop-download'))
    await waitFor(() => expect(clicked).toHaveLength(1), T)
    expect(clicked[0].getAttribute('download')).toMatch(/^drop-demo-obj-\d+-v1$/)
  })
})

describe('documents', () => {
  it('lists versions with "N of 1024", the cap reached, pruned versions disabled; files-only hosts offer no new version', async () => {
    open('drop-versions')
    const panel = await screen.findByTestId('drop-versions', {}, T)
    expect(within(panel).getByTestId('drop-versions-count')).toHaveTextContent('1024 of 1024 versions')
    expect(within(panel).getByTestId('drop-version-cap')).toHaveTextContent('This document has reached 1024 versions.')
    const versions = await within(panel).findAllByTestId('drop-version', {}, T)
    expect(versions).toHaveLength(12)
    expect(within(versions[0]).getByTestId('drop-version-download')).toBeEnabled()
    expect(within(versions[9]).getByTestId('drop-version-download')).toBeDisabled()
    expect(within(panel).getByTestId('drop-documents-readonly')).toHaveTextContent('New versions of a document are saved from a phone. This host sends files only.')
    expect(within(panel).queryByTestId('drop-save-version')).toBeNull()
  })

  it('with document writes on, a full document still offers no new version', async () => {
    open('drop-versions', { documentWrites: true })
    const panel = await screen.findByTestId('drop-versions', {}, T)
    expect(within(panel).getByTestId('drop-version-cap')).toBeInTheDocument()
    expect(within(panel).queryByTestId('drop-save-version')).toBeNull()
    expect(within(panel).queryByTestId('drop-documents-readonly')).toBeNull()
  })

  it('with document writes on, saving sends base_version; a conflict says reload', async () => {
    const view = open('drop-versions', { documentWrites: true })
    const row = await rowOf('meeting-notes.md')
    await view.user.click(within(row).getByTestId('drop-versions-toggle'))
    const panel = await within(row).findByTestId('drop-versions', {}, T)
    expect(within(panel).getByTestId('drop-versions-count')).toHaveTextContent('37 of 1024 versions')
    // Someone else saved version 38 first.
    act(() => fixture().items('received').splice(1, 1, { ...fixture().items('received')[1], version: 38, versions: 38 }))
    await view.user.upload(within(panel).getByTestId('drop-file-input'), file(5, 'notes-v2.md'))
    const err = await within(panel).findByTestId('drop-error', {}, T)
    expect(err).toHaveAttribute('data-code', 'drop.version_conflict')
    expect(err).toHaveTextContent('Someone saved a newer version; reload.')
    expect(ops('send')[0].args[0]).toMatchObject({ object_id: 'demo-doc-notes', base_version: 37 })
  })
})

describe('delete', () => {
  it.each(['demo-current-head', null])('sends the listed version and head (%s), keeps conflicts open and refreshes', async (head) => {
    const item = dropItem({ name: 'changed.pdf', version: 7, head })
    const view = open('drop-list', { received: [item], retireConflict: true })
    await view.user.click(within(await rowOf('changed.pdf')).getByTestId('drop-delete'))
    const listsBefore = ops('list').length
    await view.user.click(await screen.findByTestId('drop-confirm-delete', {}, T))
    const dialog = screen.getByTestId('drop-confirm')
    await waitFor(() => expect(dialog).toHaveTextContent('This item changed in the meantime. Reload the list and try again.'))
    expect(within(dialog).getByTestId('drop-error')).toHaveAttribute('data-code', 'drop.version_conflict')
    expect(ops('retire')[0].args).toEqual([{ object_id: item.object_id, expect_version: 7, expect_head: head }])
    await waitFor(() => expect(ops('list').length).toBeGreaterThan(listsBefore))
    expect(dialog).toBeInTheDocument()
    expect(fixture().items('received')[0].state).toBe('live')
  })

  it('shows removal dates beside both tombstone reasons, without a line for live items', async () => {
    open('drop-list')
    for (const name of ['expired-photo.heic', 'deleted-contract.pdf']) {
      const row = await rowOf(name)
      const item = fixture().items('received').find((i) => i.name === name)!
      expect(within(row).getByTestId('drop-download-reason')).toHaveTextContent(
        `Disappears from this list on ${fmtDateTime(new Date(item.tombstone_until_ms!).toISOString())}`,
      )
    }
    expect(within(await rowOf('quarterly-report.pdf')).queryByTestId('drop-tombstone-until')).toBeNull()
    expect(fixture().items('received').find((i) => i.name === 'quarterly-report.pdf')!.tombstone_until_ms).toBeNull()
  })

  it('asks first, says it is removed for everyone at once; Keep it does nothing; Delete retires it', async () => {
    const view = open('drop-list')
    const row = await rowOf('quarterly-report.pdf')
    await view.user.click(within(row).getByTestId('drop-delete'))
    const dialog = await screen.findByTestId('drop-confirm', {}, T)
    expect(dialog).toHaveTextContent('Delete this file for everyone?')
    expect(dialog).toHaveTextContent('It is removed for everyone at once.')
    await view.user.click(within(dialog).getByRole('button', { name: 'Keep it' }))
    await waitFor(() => expect(screen.queryByTestId('drop-confirm')).toBeNull())
    expect(ops('retire')).toHaveLength(0)
    await view.user.click(within(row).getByTestId('drop-delete'))
    await view.user.click(await screen.findByTestId('drop-confirm-delete', {}, T))
    await waitFor(() => expect(ops('retire')).toHaveLength(1))
    await waitFor(async () => expect(within(await rowOf('quarterly-report.pdf')).getByTestId('drop-state')).toHaveTextContent('Deleted'), T)
    expect(within(await rowOf('quarterly-report.pdf')).getByTestId('drop-download')).toBeDisabled()
  })

  it('a document delete names its versions', async () => {
    const view = open('drop-list')
    await view.user.click(within(await rowOf('meeting-notes.md')).getByTestId('drop-delete'))
    expect(await screen.findByTestId('drop-confirm', {}, T)).toHaveTextContent('Delete this document for everyone?It is removed for everyone at once, with all its versions.')
  })
})

describe('review fixes', () => {
  it('L1: the saved name is the host name only when plain; otherwise the fixed fallback', () => {
    const fb = 'drop-x-v1'
    expect(downloadName('quarterly-report.pdf', fb)).toBe('quarterly-report.pdf')
    expect(downloadName('Übersicht 2026.pdf', fb)).toBe('Übersicht 2026.pdf')
    for (const bad of ['invoice\u202Efdp.exe', 'a\u200Bb.txt', 'tab\there.txt', 'line\nbreak.txt', '<img src=x onerror=alert(1)>.html', '../etc/passwd', 'a\\b.txt',
      'C:evil.txt', ' lead.txt', 'trail.txt ', '.hidden', 'name.', 'CON', 'con.txt', 'LPT1.log', 'open-me.lnk', 'run.command', 'app.desktop', 'x'.repeat(MAX_FILE_NAME + 1), 'e\u0301.txt'])
      expect(downloadName(bad, fb), JSON.stringify(bad)).toBe(fb)
    expect(downloadName(null, fb)).toBe(fb)
    expect(downloadName('', fb)).toBe(fb)
  })

  it('L1: a right-to-left override name downloads under the fallback name', async () => {
    const view = open('drop-long-names')
    const row = await rowOf('invoice\\u{202e}fdp.exe')
    await view.user.click(within(row).getByTestId('drop-download'))
    await waitFor(() => expect(clicked).toHaveLength(1), T)
    expect(clicked[0].getAttribute('download')).toMatch(/^drop-demo-obj-\d+-v1$/)
  })

  it('L2: the bytes actually read are checked against the cap; nothing is sent when they exceed it', async () => {
    const fx = createDropFixture()
    const undo = installDropFixture(fx)
    try {
      const f = file(10, 'grew.bin')
      // The file grew on disk between the pick (size 10) and the read.
      Object.defineProperty(f, 'arrayBuffer', { value: async () => new Uint8Array(2000).buffer })
      await expect(uploadBlob('w', f, { maxBytes: 1000 })).rejects.toMatchObject({ code: 'drop.too_large' })
      expect(fx.calls.filter((c) => c.op === 'blob.put')).toHaveLength(0)
    } finally {
      undo()
    }
  })

  it('L3: a blob.put failure part-way drops the allocated host blob', async () => {
    const view = open('drop-list', { putErrorAtChunk: 1 })
    await pick(view, file(2 * MiB, 'fails.bin'))
    expect(await within(screen.getByTestId('drop-zone')).findByTestId('drop-error', {}, T)).toHaveAttribute('data-code', 'blob.invalid')
    await waitFor(() => expect(ops('blob.drop')).toHaveLength(1))
    expect(ops('blob.drop')[0].args[0]).toMatch(/^b_DEMO/)
    expect(ops('send')).toHaveLength(0)
  }, BIG_FILE_MS)

  it('L3: a refused drop.send drops the uploaded blob', async () => {
    const view = open('drop-errors')
    await pick(view, file(5, 'x.txt'))
    await within(screen.getByTestId('drop-zone')).findByTestId('drop-error', {}, T)
    await waitFor(() => expect(ops('blob.drop')).toHaveLength(1))
    expect(ops('blob.drop')[0].args[0]).toBe((ops('send')[0].args[0] as { blob: string }).blob)
  })

  it('L3: the decrypted read blob is dropped after the download', async () => {
    const view = open('drop-list')
    await view.user.click(within(await rowOf('quarterly-report.pdf')).getByTestId('drop-download'))
    await waitFor(() => expect(ops('blob.drop')).toHaveLength(1), T)
    expect(ops('blob.drop')[0].args[0]).toMatch(/^b_READ/)
  })

  describe('L4: progress follows the host\'s received', () => {
    const run = async (answer: (args: { offset: number; b64: string; blob?: string }, n: number) => number) => {
      const offsets: number[] = []
      const progress: number[] = []
      let n = 0
      const undo = installDropFixture(createDropFixture())
      dropApi.blobPut = async (_ws, args) => {
        offsets.push(args.offset)
        return { blob: 'b_TEST', size: 0, received: answer(args, n++) }
      }
      try {
        const result = uploadBlob('w', file(2 * CHUNK_BYTES, 'x.bin'), { maxBytes: 100 * MiB, onProgress: (sent) => progress.push(sent) })
        return { result: await result.then(() => 'ok', (e) => e), offsets, progress }
      } finally {
        undo()
      }
    }
    const len = (b64: string) => atob(b64).length

    it('a host that lags is continued from where it is', async () => {
      const r = await run((a, n) => (n === 0 ? a.offset + len(a.b64) - 100 : a.offset + len(a.b64)))
      expect(r.result).toBe('ok')
      expect(r.offsets).toEqual([0, CHUNK_BYTES - 100, 2 * CHUNK_BYTES - 100])
      expect(r.progress).toEqual([0, CHUNK_BYTES - 100, 2 * CHUNK_BYTES - 100, 2 * CHUNK_BYTES])
    })

    it('an overshooting received never shows more than the file', async () => {
      const r = await run(() => 10 * CHUNK_BYTES)
      expect(r.result).toBe('ok')
      expect(Math.max(...r.progress)).toBe(2 * CHUNK_BYTES)
    })

    it('a host that goes backwards is an error, not a silent restart', async () => {
      const r = await run((a, n) => (n === 0 ? a.offset + len(a.b64) : 0))
      expect(r.result).toMatchObject({ code: 'blob.invalid' })
      expect(r.offsets).toEqual([0, CHUNK_BYTES])
    })
  })

  it('L6: Delete is hidden where the host says it would refuse; a refusal is a mapped sentence', async () => {
    const view = open('drop-list', {
      received: [dropItem({ name: 'not-mine.pdf', can_retire: false }), dropItem({ name: 'own.pdf' })],
      retireError: new DropError(403, { code: 'drop.not_eligible', message: 'drop.not_eligible', retryable: false }),
    })
    expect(within(await rowOf('not-mine.pdf')).queryByTestId('drop-delete')).toBeNull()
    await view.user.click(within(await rowOf('own.pdf')).getByTestId('drop-delete'))
    await view.user.click(await screen.findByTestId('drop-confirm-delete', {}, T))
    const err = await within(screen.getByTestId('drop-confirm')).findByTestId('drop-error', {}, T)
    expect(err).toHaveAttribute('data-code', 'drop.not_eligible')
    expect(err.textContent).not.toMatch(/^Not done\./)
  })

  it('I1: host ids are shown with invisible characters escaped', async () => {
    open('drop-list', { received: [dropItem({ name: 'x.pdf', author: { ...PHONE, person_name: null, person: 'p_\u202Eevil', device: 'd_\u202Edev' } })] })
    const author = within(await rowOf('x.pdf')).getByTestId('drop-author')
    expect(author.textContent).toContain('p_\\u{202e}evil')
    expect(author.textContent).toContain('d_\\u{202e}dev')
    expect(author.textContent).not.toMatch(/\u202e/)
  })

  it('I3: the object URL is revoked after the download', () => {
    vi.useFakeTimers()
    try {
      saveBytes(new Blob(['x']), 'a.txt', 'fb')
      expect(URL.revokeObjectURL).not.toHaveBeenCalled()
      vi.advanceTimersByTime(30_000)
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:demo/1')
    } finally {
      vi.useRealTimers()
    }
  })

  it('I4: "not served" has its own code and text, distinct from the relay being unreachable', async () => {
    const e = await realDropApi.info('w').catch((x) => x)
    expect(dropError(e)).toEqual({ code: 'drop.not_served', text: 'Drop is not available on this host yet.' })
    expect(dropError(DROP_SAMPLE_ERRORS[8]).text).toMatch(/can't reach the relay/)
  })
})

describe('the upload-progress preview', () => {
  it('holds mid-upload under StrictMode (as in the browser): progress at 50 % and Cancel, until cancelled', async () => {
    dropPreviewForTests.set(null)
    const view = renderApp('/__layout/drop-upload-progress', { strict: true })
    await waitFor(() => expect(screen.getByTestId('drop-progress-text')).toHaveTextContent('1.5 MiB of 3 MiB (50 %)'), T)
    await new Promise((r) => setTimeout(r, 300))
    expect(screen.getByTestId('drop-progress')).toBeInTheDocument()
    expect(screen.queryByTestId('drop-cancelled')).toBeNull()
    await view.user.click(screen.getByTestId('drop-cancel'))
    expect(await screen.findByTestId('drop-cancelled', {}, T)).toBeInTheDocument()
  })
})

describe('Drop A2: shared from your phone', () => {
  const shared = (over: Parameters<typeof dropItem>[0]) =>
    dropItem({ shared_from: PHONE_SHARE, claimed_ms: Date.parse('2026-10-09T10:00:00Z'), shared_until_ms: Date.parse('2026-10-16T10:00:00Z'), ...over })

  it('the tab is a view filter over the same rows, with label, claim time, shared-until and the gestureless note', async () => {
    const view = open('drop-phone')
    await rowOf('plain-file.pdf')
    await view.user.click(screen.getByTestId('drop-tab-shared'))
    await waitFor(() => expect(screen.queryByText('plain-file.pdf')).toBeNull(), T)
    expect(ops('list').at(-1)!.args).toEqual(['received', null, 'shared_from_phone'])
    const row = await rowOf('boarding-pass.pdf')
    expect(within(row).getByTestId('drop-shared-label')).toHaveTextContent('Severin’s iPhone')
    expect(within(row).getByTestId('drop-claimed')).toBeInTheDocument()
    expect(within(row).getByTestId('drop-shared-until').textContent).toMatch(/^· shared until /)
    expect(within(row).queryByTestId('drop-gestureless')).toBeNull()
    expect(within(await rowOf('photo-without-confirmation.jpg')).getByTestId('drop-gestureless')).toHaveTextContent('added from your phone without a confirmation')
  })

  it('the label is text, never a link or HTML', async () => {
    const view = open('drop-list', { received: [shared({ name: 'x.pdf', shared_from: { device: 'd_fixture_x', label: '<a href="https://evil.test">iPhone</a>' } })] })
    await view.user.click(await screen.findByTestId('drop-tab-shared'))
    const row = await rowOf('x.pdf')
    expect(within(row).getByTestId('drop-shared-label').textContent).toContain('<a href="https://evil.test">iPhone</a>')
    expect(row.querySelector('a')).toBeNull()
  })

  it('expired and deleted shares stay listed with "no longer available" and no Download or Delete', async () => {
    const view = open('drop-phone')
    await view.user.click(await screen.findByTestId('drop-tab-shared'))
    const expired = await rowOf('old-scan.pdf')
    expect(within(expired).getByTestId('drop-gone')).toHaveTextContent('File no longer available (expired)')
    const gone = await rowOf('removed-by-owner.pdf')
    expect(within(gone).getByTestId('drop-gone')).toHaveTextContent('File no longer available (deleted)')
    for (const r of [expired, gone]) {
      expect(within(r).queryByTestId('drop-download')).toBeNull()
      expect(within(r).queryByTestId('drop-delete')).toBeNull()
      expect(within(r).getByTestId('drop-shared-until')).toBeInTheDocument()
    }
    expect(within(await rowOf('boarding-pass.pdf')).queryByTestId('drop-gone')).toBeNull()
  })

  it('each unclaimed_reason has its own text; an unknown one gets the generic text', async () => {
    open('drop-phone')
    const text = async (n: string) => within(await rowOf(n)).getByTestId('drop-download-reason').textContent
    expect(await text('awaiting.jpg')).toMatch(/until this workspace’s host claims it/)
    expect(await text('revoked-phone.jpg')).toMatch(/phone was removed before this was claimed, so the file can’t be claimed/)
    expect(await text('needs-terminal.jpg')).toMatch(/computer’s terminal/)
    expect(await text('from-the-future.jpg')).toMatch(/Not claimed yet/)
  })

  it('pending_others: a count line when N > 0, nothing when 0 or absent; rows are never filtered client-side', async () => {
    open('drop-phone')
    expect(await screen.findByTestId('drop-pending-others')).toHaveTextContent('3 items from others await review at this computer\'s terminal.')
    cleanup()
    open('drop-phone', { pendingOthers: 0 })
    await rowOf('plain-file.pdf')
    expect(screen.queryByTestId('drop-pending-others')).toBeNull()
    cleanup()
    open('drop-list')
    await rowOf('quarterly-report.pdf')
    expect(screen.queryByTestId('drop-pending-others')).toBeNull()
  })

  it('can_retire: self deletes directly, handoff shows the terminal path, null shows nothing', async () => {
    open('drop-phone')
    expect(within(await rowOf('boarding-pass.pdf')).getByTestId('drop-delete')).toBeInTheDocument()
    const hand = await rowOf('needs-terminal.jpg')
    expect(within(hand).queryByTestId('drop-delete')).toBeNull()
    expect(within(hand).getByTestId('drop-handoff')).toHaveTextContent("Confirm at this computer's terminal")
    const none = await rowOf('awaiting.jpg')
    expect(within(none).queryByTestId('drop-delete')).toBeNull()
    expect(within(none).queryByTestId('drop-handoff')).toBeNull()
  })

  it('drop.handoff_required in the confirmation switches to the hand-off text; role.denied is the denied text', async () => {
    const view = open('drop-list', {
      received: [dropItem({ name: 'own.pdf' })],
      retireError: new DropError(409, { code: 'drop.handoff_required', message: 'drop.handoff_required', retryable: false }),
    })
    await view.user.click(within(await rowOf('own.pdf')).getByTestId('drop-delete'))
    await view.user.click(await screen.findByTestId('drop-confirm-delete', {}, T))
    expect(await within(screen.getByTestId('drop-confirm')).findByTestId('drop-handoff', {}, T)).toHaveTextContent("Confirm at this computer's terminal")
    expect(screen.queryByTestId('drop-confirm-delete')).toBeNull()
    const denied = dropError(new DropError(403, { code: 'role.denied', message: 'role.denied', retryable: false }))
    expect(denied.text).toBe(dropError(new DropError(403, { code: 'drop.not_eligible', message: 'x', retryable: false })).text)
  })

  it('role.denied in the confirmation dialog shows the denied text', async () => {
    const view = open('drop-list', {
      received: [dropItem({ name: 'own.pdf' })],
      retireError: new DropError(403, { code: 'role.denied', message: 'role.denied', retryable: false }),
    })
    await view.user.click(within(await rowOf('own.pdf')).getByTestId('drop-delete'))
    await view.user.click(await screen.findByTestId('drop-confirm-delete', {}, T))
    const err = await within(screen.getByTestId('drop-confirm')).findByTestId('drop-error', {}, T)
    expect(err).toHaveAttribute('data-code', 'role.denied')
    expect(err.textContent).toMatch(/You can't do this to this item/)
    expect(screen.queryByTestId('drop-handoff')).toBeNull()
  })

  it('drop.cursor_reset reloads page 1 quietly with one notice; no cursor is shown', async () => {
    const view = open('drop-list', { cursorResetOnSecondPage: true })
    await rowOf('quarterly-report.pdf')
    await view.user.click(screen.getByTestId('drop-next'))
    expect(await screen.findByTestId('drop-refreshed', {}, T)).toHaveTextContent('The list was refreshed.')
    await rowOf('quarterly-report.pdf')
    expect(screen.queryByTestId('drop-error')).toBeNull()
    expect(screen.getAllByTestId('drop-row')).toHaveLength(8)
    expect(document.body.textContent).not.toMatch(/\bcursor\b/i)
    await view.user.click(screen.getByTestId('drop-next'))
    await waitFor(() => expect(screen.getAllByTestId('drop-row')).toHaveLength(3), T)
    expect(screen.queryByTestId('drop-refreshed')).toBeNull()
  })

  it('old hosts: no new fields means the old behaviour; can_retire false still hides Delete', async () => {
    open('drop-list', { received: [dropItem({ name: 'old.pdf', can_retire: false }), dropItem({ name: 'older.pdf' })] })
    expect(within(await rowOf('old.pdf')).queryByTestId('drop-delete')).toBeNull()
    expect(within(await rowOf('older.pdf')).getByTestId('drop-delete')).toBeInTheDocument()
    expect(screen.queryByTestId('drop-shared')).toBeNull()
  })

  it('M1: a host that always answers cursor_reset ends in the error state with Retry, after at most one quiet reset', async () => {
    const view = open('drop-list', { pageSize: 8 })
    await rowOf('quarterly-report.pdf')
    // From here the host is faulty: every list call (with or without a cursor) is a reset.
    let n = 0
    dropApi.list = async () => {
      n += 1
      throw new DropError(410, { code: 'drop.cursor_reset', message: 'drop.cursor_reset', retryable: false })
    }
    await view.user.click(screen.getByTestId('drop-next'))
    const err = await screen.findByTestId('drop-error', {}, T)
    expect(err).toHaveAttribute('data-code', 'drop.cursor_reset')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText('Reading the list…')).toBeNull()
    const settled = n
    await new Promise((r) => setTimeout(r, 400))
    expect(n).toBe(settled) // it stopped; nothing keeps reloading
    expect(settled).toBeLessThanOrEqual(3) // the cursor call, then one quiet reset to page 1 (which may refetch once)
  })

  it('M1b: with the fixture option a page-1 reset is the normal error', async () => {
    open('drop-list', { cursorResetAlways: true })
    expect(await screen.findByTestId('drop-error', {}, T)).toHaveAttribute('data-code', 'drop.cursor_reset')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(ops('list')).toHaveLength(1)
  })

  it('M2: shared_from without a string label, or not an object, does not crash', async () => {
    const odd = (name: string, shared_from: unknown) => dropItem({ name, shared_from: shared_from as never })
    const view = open('drop-list', {
      received: [odd('nolabel.pdf', { device: 'd_fixture_a' }), odd('numlabel.pdf', { device: 'd_fixture_b', label: 42 }), odd('nulllabel.pdf', { device: 'd_fixture_c', label: null }), odd('str.pdf', 'phone'), odd('num.pdf', 7)],
    })
    await view.user.click(await screen.findByTestId('drop-tab-shared'))
    for (const n of ['nolabel.pdf', 'numlabel.pdf', 'nulllabel.pdf']) expect(within(await rowOf(n)).getByTestId('drop-shared-label')).toHaveTextContent('your phone')
    expect(within(await rowOf('str.pdf')).queryByTestId('drop-shared')).toBeNull()
    expect(within(await rowOf('num.pdf')).queryByTestId('drop-shared')).toBeNull()
  })

  it('L1/L2: a long label is one line; an RTL-override and control-character label is neutralized inside a bdi', async () => {
    const view = open('drop-list', {
      received: [
        shared({ name: 'long.pdf', shared_from: { device: 'd_fixture_l', label: 'iPhone '.repeat(40) } }),
        shared({ name: 'bidi.pdf', shared_from: { device: 'd_fixture_b', label: 'Pho\u202Eene\u0007\u200Bx' } }),
      ],
    })
    await view.user.click(await screen.findByTestId('drop-tab-shared'))
    const long = within(await rowOf('long.pdf')).getByTestId('drop-shared-label')
    expect(long.className).toMatch(/truncate/)
    expect(long.querySelector('bdi')!.className).toMatch(/!whitespace-nowrap/)
    const bidi = within(await rowOf('bidi.pdf')).getByTestId('drop-shared-label')
    expect(bidi.querySelector('bdi')).not.toBeNull()
    expect(bidi.textContent).toBe('Pho\\u{202e}ene\\u{7}\\u{200b}x')
    expect(bidi.textContent).not.toMatch(/[\u202e\u0007\u200b]/)
  })
})
