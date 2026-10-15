// Workspace → Drop on a real host (PROPOSED ops, cap `drop/1`, behind the feature gate; src/api/drop.ts). Files between
// this workspace and its members' devices, sealed by the HOST: the page uploads plaintext to a host blob, the host seals
// and sends; the host verifies and decrypts, the page only ever downloads.
//
// Everything an author chose (name, MIME type, content) is untrusted. Names and types are rendered as text through
// visible.tsx (the host cleans them; the page cleans nothing). Content is never rendered: no preview, no inline view, no
// new tab; a download is an `application/octet-stream` blob behind an anchor with `download`.
import { useCallback, useEffect, useRef, useState, type DragEvent, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { DROP_UNAVAILABLE, dropApi, dropError, dropQueries, dropServed, fmtBytes, newIdem, saveBytes, sizeRefusal, uploadBlob, UploadCancelled, type DropDirection, type DropErrorDetail, type DropInfo, type DropItem, type DropState, type DropVersion } from '@/api/drop'
import type { Workspace } from '@/api/types'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { fmtDateTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'
import { Mono, Pill } from '../ticket/shared'

const box = 'space-y-2 rounded-md border border-border px-3 py-3 text-[13px]'
const when = (ms: number) => fmtDateTime(new Date(ms).toISOString())

/** The route's page: the current workspace. */
export function DropPage() {
  usePageHeader('Drop')
  const { workspace } = useWorkspace()
  if (!workspace) return null
  return <DropView workspace={workspace} />
}

/** Workspace → Drop. Without the gate: one calm line, no call to the host. */
/** DEV preview only (src/dev/layoutPreview.tsx): start with a document's versions open, or with an upload running. */
export interface DropPreviewStart {
  openDoc?: string
  upload?: File
}

export function DropView({ workspace, preview }: { workspace: Workspace; preview?: DropPreviewStart }) {
  return (
    <div className="w-full max-w-4xl min-w-0 space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Drop</h1>
      {dropServed(workspace.id) ? (
        <Drop ws={workspace.id} preview={preview} />
      ) : (
        <p data-testid="drop-unavailable" className="rounded-md border border-border px-3 py-2 text-[13px] text-text-muted">
          {DROP_UNAVAILABLE}
        </p>
      )}
    </div>
  )
}

/** A Drop error: the core sentence and the code; an unknown quota name as untrusted text. */
export function DropErrorLine({ detail, children }: { detail: DropErrorDetail; children?: ReactNode }) {
  return (
    <div role="alert" data-testid="drop-error" data-code={detail.code} className="space-y-1 text-[13px] text-danger">
      <p>
        {detail.text} <Mono className="text-text-muted">{detail.code}</Mono>
        {detail.quota !== undefined && (
          <>
            {' '}
            Quota: <Inline>{detail.quota}</Inline>
          </>
        )}
      </p>
      {children}
    </div>
  )
}

function Drop({ ws, preview }: { ws: string; preview?: DropPreviewStart }) {
  const qc = useQueryClient()
  const info = useQuery(dropQueries.info(ws))
  const [direction, setDirection] = useState<DropDirection>('received')
  // Cursor pages: `after` of the shown page, and the ones before it for "Newer".
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const after = cursors[cursors.length - 1]
  const list = useQuery(dropQueries.list(ws, direction, after))
  const [retiring, setRetiring] = useState<DropItem | null>(null)
  const [openDoc, setOpenDoc] = useState<string | null>(preview?.openDoc ?? null)

  const refresh = useCallback(() => void qc.invalidateQueries({ queryKey: ['drop', ws] }), [qc, ws])
  // Live: a `drop` sub item says something changed; the page refetches, it never polls.
  useEffect(() => dropApi.subscribe(ws, () => refresh()), [ws, refresh])

  const pick = (d: DropDirection) => {
    setDirection(d)
    setCursors([null])
    setOpenDoc(null)
  }

  if (info.isError) return <DropErrorLine detail={dropError(info.error)}><Button variant="outline" size="sm" onClick={() => void info.refetch()}>Retry</Button></DropErrorLine>
  if (!info.data)
    return (
      <p role="status" className="text-[13px] text-text-muted">
        Reading Drop…
      </p>
    )
  const limits = info.data
  const items = list.data?.items ?? []

  return (
    <>
      <p className="text-[13px] text-text-muted">
        Files between this workspace and its members&apos; devices, end-to-end encrypted. This Mac&apos;s host seals what you send and opens what you
        receive; the relay only stores ciphertext. Up to <span data-testid="drop-limit">{fmtBytes(limits.max_bytes)}</span> per file; files expire after 7 days.
      </p>
      <Upload ws={ws} info={limits} onSent={refresh} initial={preview?.upload} />

      <div role="tablist" aria-label="Drop" className="flex gap-1 border-b border-border">
        {(['received', 'sent'] as const).map((d) => (
          <button
            key={d}
            role="tab"
            type="button"
            data-testid={`drop-tab-${d}`}
            aria-selected={direction === d}
            onClick={() => pick(d)}
            className={cn('-mb-px border-b-2 px-3 py-1.5 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-brand', direction === d ? 'border-brand text-text' : 'border-transparent text-text-muted hover:text-text')}
          >
            {d === 'received' ? 'Received' : 'Sent'}
          </button>
        ))}
      </div>

      {list.isError ? (
        <DropErrorLine detail={dropError(list.error, limits)}>
          <Button variant="outline" size="sm" onClick={() => void list.refetch()}>
            Retry
          </Button>
        </DropErrorLine>
      ) : list.isPending ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the list…
        </p>
      ) : items.length === 0 ? (
        <p data-testid="drop-empty" className="text-[13px] text-text-muted">
          {direction === 'received' ? 'Nothing has been dropped to this workspace.' : 'Nothing has been sent from this workspace.'}
        </p>
      ) : (
        <ul aria-label={direction === 'received' ? 'Received' : 'Sent'} className="divide-y divide-border rounded-lg border border-border bg-surface">
          {items.map((item) => (
            <Row
              key={item.object_id}
              ws={ws}
              item={item}
              info={limits}
              open={openDoc === item.object_id}
              onToggle={() => setOpenDoc((o) => (o === item.object_id ? null : item.object_id))}
              onRetire={() => setRetiring(item)}
              onChanged={refresh}
            />
          ))}
        </ul>
      )}
      {(cursors.length > 1 || list.data?.next) && (
        <div className="flex gap-2">
          <Button variant="outline" size="sm" data-testid="drop-prev" disabled={cursors.length < 2} onClick={() => setCursors((c) => c.slice(0, -1))}>
            Newer
          </Button>
          <Button variant="outline" size="sm" data-testid="drop-next" disabled={!list.data?.next} onClick={() => list.data?.next && setCursors((c) => [...c, list.data!.next])}>
            Older
          </Button>
        </div>
      )}
      {retiring && <RetireDialog ws={ws} item={retiring} onClose={() => setRetiring(null)} onDone={refresh} />}
    </>
  )
}

// ——— The name and the author ———

/** An author-chosen name: untrusted text, every invisible character shown; a hidden one is never guessed at. */
export function DropName({ name, hidden }: { name: string | null; hidden: boolean }) {
  if (hidden || name === null)
    return (
      <span data-testid="drop-name-hidden" className="italic text-text-muted">
        a name that can&apos;t be shown
      </span>
    )
  return (
    <span data-testid="drop-name" className="font-medium">
      <Inline>{name}</Inline>
    </span>
  )
}

function Author({ author }: { author: DropItem['author'] }) {
  return (
    <span data-testid="drop-author">
      {author.person_name ? <Inline>{author.person_name}</Inline> : <Mono>{author.person}</Mono>} · device <Mono>{author.device}</Mono>
      {author.origin === 'agent' ? (
        <Pill tone="warning" className="ml-1.5">
          <span data-testid="drop-agent">sealed by an agent</span>
        </Pill>
      ) : author.origin === 'phone' ? (
        ' · from a phone'
      ) : (
        ' · by the person'
      )}
    </span>
  )
}

const STATE_WORD: Record<DropState, string> = { live: 'Live', pruned: 'Content pruned', expired: 'Expired', retired: 'Deleted' }
const STATE_TONE: Record<DropState, 'success' | 'warning' | 'neutral' | 'danger'> = { live: 'success', pruned: 'warning', expired: 'neutral', retired: 'neutral' }

/** Why the download is off, or null when it works. */
export function downloadBlocked(item: Pick<DropItem, 'state' | 'claimed'>): string | null {
  switch (item.state) {
    case 'pruned':
      return 'The content of this version was pruned: only the newest 8 versions and those under 30 days old keep it.'
    case 'expired':
      return 'This file expired: its content is gone. The entry stays listed for 30 days.'
    case 'retired':
      return 'This was deleted for everyone. The entry stays listed for 30 days.'
  }
  if (!item.claimed) return 'Not claimed yet: until this workspace’s host claims it, only the host and the sender can open it.'
  return null
}

// ——— A row ———

function Row({ ws, item, info, open, onToggle, onRetire, onChanged }: { ws: string; item: DropItem; info: DropInfo; open: boolean; onToggle: () => void; onRetire: () => void; onChanged: () => void }) {
  const blocked = downloadBlocked(item)
  const tomb = item.state === 'expired' || item.state === 'retired'
  const [error, setError] = useState<DropErrorDetail | null>(null)
  const [busy, setBusy] = useState(false)
  const download = async () => {
    setError(null)
    setBusy(true)
    try {
      await downloadVersion(ws, item.object_id, item.version)
    } catch (e) {
      setError(dropError(e, info))
    } finally {
      setBusy(false)
    }
  }
  return (
    <li data-testid="drop-row" data-object={item.object_id} data-state={item.state} className={cn('min-w-0 space-y-1.5 px-3 py-2.5 text-[13px]', tomb && 'opacity-80')}>
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1 [overflow-wrap:anywhere]">
          <DropName name={item.name} hidden={item.name_hidden} />
        </div>
        <div className="flex shrink-0 flex-wrap gap-1.5">
          <Button size="sm" variant="outline" data-testid="drop-download" disabled={blocked !== null || busy} aria-describedby={blocked ? `why-${item.object_id}` : undefined} onClick={() => void download()}>
            {busy ? 'Downloading…' : 'Download'}
          </Button>
          {!tomb && (
            <Button size="sm" variant="outline" data-testid="drop-delete" onClick={onRetire}>
              Delete
            </Button>
          )}
        </div>
      </div>
      <dl className="flex min-w-0 flex-wrap gap-x-4 gap-y-1 text-[12px] text-text-muted [&_dt]:sr-only">
        <div>
          <dt>Kind</dt>
          <dd data-testid="drop-kind">
            {item.kind === 'document' ? `Document, version ${item.version}` : 'File'}
            {item.mime && (
              <>
                {' '}
                · <Inline>{item.mime}</Inline>
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>Size</dt>
          <dd data-testid="drop-size">{fmtBytes(item.size)}</dd>
        </div>
        <div className="min-w-0 [overflow-wrap:anywhere]">
          <dt>Author</dt>
          <dd>
            <Author author={item.author} />
          </dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>Created {when(item.created_ms)}</dd>
        </div>
        <div>
          <dt>Expiry</dt>
          <dd data-testid="drop-expiry">{item.expires_ms === null ? 'No expiry' : `${item.state === 'expired' ? 'Expired' : 'Expires'} ${when(item.expires_ms)}`}</dd>
        </div>
        <div>
          <dt>State</dt>
          <dd className="flex flex-wrap gap-1">
            <Pill tone={STATE_TONE[item.state]}>
              <span data-testid="drop-state">{STATE_WORD[item.state]}</span>
            </Pill>
            {!item.claimed && !tomb && (
              <Pill tone="info">
                <span data-testid="drop-unclaimed">Unclaimed</span>
              </Pill>
            )}
          </dd>
        </div>
      </dl>
      {blocked && (
        <p id={`why-${item.object_id}`} data-testid="drop-download-reason" className="text-[12px] text-text-muted">
          {blocked}
        </p>
      )}
      {item.kind === 'document' && (
        <button type="button" data-testid="drop-versions-toggle" aria-expanded={open} onClick={onToggle} className="text-[12px] text-brand underline-offset-2 hover:underline">
          {open ? 'Hide versions' : `Versions (${item.versions} of ${info.version_cap})`}
        </button>
      )}
      {error && <DropErrorLine detail={error} />}
      {open && <Versions ws={ws} item={item} info={info} onChanged={onChanged} />}
    </li>
  )
}

/** `drop.read` then the bytes, saved as a download. Never inline, never a new tab. */
async function downloadVersion(ws: string, object_id: string, version: number) {
  const r = await dropApi.read(ws, { object_id, version })
  const bytes = await dropApi.content(ws, r.blob)
  saveBytes(bytes, r.name_hidden ? null : r.name, `drop-${object_id}-v${version}`)
}

// ——— Document versions ———

function Versions({ ws, item, info, onChanged }: { ws: string; item: DropItem; info: DropInfo; onChanged: () => void }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const page = useQuery(dropQueries.versions(ws, item.object_id, cursors[cursors.length - 1]))
  const cap = page.data?.cap ?? info.version_cap
  const full = item.versions >= cap
  const writes = info.writes.includes('document')
  return (
    <section data-testid="drop-versions" aria-label="Versions" className={cn(box, 'bg-surface-2')}>
      <p>
        <span data-testid="drop-versions-count">
          {item.versions} of {cap} versions
        </span>
        <span className="text-text-muted"> · a document keeps at most {cap} versions; older ones keep their content only while among the newest 8 or under 30 days old.</span>
      </p>
      {full && (
        <p data-testid="drop-version-cap" className="text-warning">
          This document has reached {cap} versions. No new version can be saved.
        </p>
      )}
      {page.isError ? (
        <DropErrorLine detail={dropError(page.error, info)} />
      ) : !page.data ? (
        <p role="status" className="text-text-muted">
          Reading the versions…
        </p>
      ) : (
        <ul className="space-y-1">
          {page.data.versions.map((v) => (
            <VersionRow key={v.version} ws={ws} object_id={item.object_id} v={v} info={info} />
          ))}
        </ul>
      )}
      {(cursors.length > 1 || page.data?.next) && (
        <div className="flex gap-2">
          <Button variant="outline" size="sm" disabled={cursors.length < 2} onClick={() => setCursors((c) => c.slice(0, -1))}>
            Newer versions
          </Button>
          <Button variant="outline" size="sm" disabled={!page.data?.next} onClick={() => page.data?.next && setCursors((c) => [...c, page.data!.next])}>
            Older versions
          </Button>
        </div>
      )}
      {!writes ? (
        <p data-testid="drop-documents-readonly" className="text-text-muted">
          New versions of a document are saved from a phone. This host sends files only.
        </p>
      ) : (
        !full && item.state === 'live' && <Upload ws={ws} info={info} onSent={onChanged} target={{ object_id: item.object_id, base_version: item.version }} />
      )}
    </section>
  )
}

function VersionRow({ ws, object_id, v, info }: { ws: string; object_id: string; v: DropVersion; info: DropInfo }) {
  const blocked = downloadBlocked({ state: v.state, claimed: true })
  const [error, setError] = useState<DropErrorDetail | null>(null)
  return (
    <li data-testid="drop-version" data-version={v.version} className="space-y-1">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium">Version {v.version}</span>
        <span className="text-text-muted">{fmtBytes(v.size)}</span>
        <span className="text-text-muted">{when(v.created_ms)}</span>
        <span className="min-w-0 text-text-muted [overflow-wrap:anywhere]">
          <Author author={v.author} />
        </span>
        <Pill tone={STATE_TONE[v.state]}>{STATE_WORD[v.state]}</Pill>
        <Button
          size="sm"
          variant="outline"
          data-testid="drop-version-download"
          disabled={blocked !== null}
          title={blocked ?? undefined}
          onClick={() => {
            setError(null)
            downloadVersion(ws, object_id, v.version).catch((e) => setError(dropError(e, info)))
          }}
        >
          Download
        </Button>
      </div>
      {blocked && <p className="text-[12px] text-text-muted">{blocked}</p>}
      {error && <DropErrorLine detail={error} />}
    </li>
  )
}

// ——— Delete (retire) ———

function RetireDialog({ ws, item, onClose, onDone }: { ws: string; item: DropItem; onClose: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<DropErrorDetail | null>(null)
  const what = item.kind === 'document' ? 'document' : 'file'
  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      await dropApi.retire(ws, item.object_id)
      onDone()
      onClose()
    } catch (e) {
      setError(dropError(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent data-testid="drop-confirm" className="max-w-lg gap-4 border-border bg-surface">
        <DialogHeader>
          <DialogTitle>Delete this {what} for everyone?</DialogTitle>
          <DialogDescription>
            It is removed for everyone at once{item.kind === 'document' ? ', with all its versions' : ''}. Nobody can download it afterwards; the entry stays listed for 30 days.
          </DialogDescription>
        </DialogHeader>
        <p className="text-[13px] [overflow-wrap:anywhere]">
          <DropName name={item.name} hidden={item.name_hidden} /> · {fmtBytes(item.size)}
        </p>
        {error && <DropErrorLine detail={error} />}
        <DialogFooter>
          <Button variant="outline" autoFocus onClick={onClose} disabled={busy}>
            Keep it
          </Button>
          <Button variant="destructive" data-testid="drop-confirm-delete" onClick={() => void run()} disabled={busy}>
            {busy ? 'Deleting…' : `Delete for everyone`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ——— Upload ———

type Phase =
  | { k: 'idle' }
  | { k: 'refused'; name: string; text: string }
  | { k: 'uploading'; name: string; sent: number; total: number }
  | { k: 'sealing'; name: string }
  | { k: 'done'; name: string }
  | { k: 'cancelled'; name: string }
  | { k: 'error'; name: string; detail: DropErrorDetail }

/** Pick or drop one file; the size is checked before anything is uploaded. `target`: a new version of a document. */
function Upload({ ws, info, onSent, target, initial }: { ws: string; info: DropInfo; onSent: () => void; target?: { object_id: string; base_version: number }; initial?: File }) {
  const [phase, setPhase] = useState<Phase>({ k: 'idle' })
  const [over, setOver] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const busy = phase.k === 'uploading' || phase.k === 'sealing'
  useEffect(() => () => abort.current?.abort(), [])

  const start = async (file: File) => {
    if (busy) return
    const refused = sizeRefusal(file.size, info)
    if (refused) return setPhase({ k: 'refused', name: file.name, text: refused })
    const ctl = new AbortController()
    abort.current = ctl
    setPhase({ k: 'uploading', name: file.name, sent: 0, total: file.size })
    let blob: string
    try {
      blob = await uploadBlob(ws, file, { signal: ctl.signal, onProgress: (sent, total) => !ctl.signal.aborted && setPhase({ k: 'uploading', name: file.name, sent, total }) })
    } catch (e) {
      abort.current = null
      return setPhase(e instanceof UploadCancelled ? { k: 'cancelled', name: file.name } : { k: 'error', name: file.name, detail: dropError(e, info) })
    }
    abort.current = null
    // The host's step: seal, sign and send to the relay. Not cancellable from here (the host persists the frame).
    setPhase({ k: 'sealing', name: file.name })
    try {
      await dropApi.send(ws, { blob, name: file.name, mime: file.type || 'application/octet-stream', ...(target ?? {}) }, newIdem())
      setPhase({ k: 'done', name: file.name })
      onSent()
    } catch (e) {
      setPhase({ k: 'error', name: file.name, detail: dropError(e, info) })
    }
  }
  const started = useRef(false)
  useEffect(() => {
    if (initial && !started.current) {
      started.current = true
      void start(initial)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once, for the dev preview
  }, [initial])
  const pickFiles = (files: FileList | null) => {
    const f = files?.[0]
    if (f) void start(f)
    if (input.current) input.current.value = ''
  }
  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setOver(false)
    pickFiles(e.dataTransfer?.files ?? null)
  }
  const label = target ? 'Save a new version' : 'Send a file'
  const id = target ? `drop-file-${target.object_id}` : 'drop-file'

  return (
    <div
      data-testid={target ? 'drop-save-version' : 'drop-zone'}
      onDragOver={(e) => {
        e.preventDefault()
        if (!busy) setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      className={cn(box, 'border-dashed', over && 'border-brand bg-brand/5')}
    >
      <div className="flex flex-wrap items-center gap-2">
        <input ref={input} id={id} data-testid="drop-file-input" type="file" className="sr-only" disabled={busy} onChange={(e) => pickFiles(e.target.files)} />
        <Button size="sm" disabled={busy} onClick={() => input.current?.click()}>
          {label}
        </Button>
        <span className="text-text-muted">or drop a file here · at most {fmtBytes(info.max_bytes)}</span>
      </div>
      <UploadStatus phase={phase} onCancel={() => abort.current?.abort()} conflict={target ? onSent : undefined} />
    </div>
  )
}

function UploadStatus({ phase, onCancel, conflict }: { phase: Phase; onCancel: () => void; conflict?: () => void }) {
  if (phase.k === 'idle') return null
  const name = <DropName name={phase.name} hidden={false} />
  switch (phase.k) {
    case 'refused':
      return (
        <p role="alert" data-testid="drop-refused" className="text-danger [overflow-wrap:anywhere]">
          {name}: {phase.text} <Mono className="text-text-muted">drop.too_large</Mono>
        </p>
      )
    case 'uploading': {
      const pct = phase.total === 0 ? 100 : Math.floor((phase.sent / phase.total) * 100)
      return (
        <div data-testid="drop-progress" className="space-y-1">
          <p className="[overflow-wrap:anywhere]">
            Uploading {name} to this Mac: <span data-testid="drop-progress-text">{fmtBytes(phase.sent)} of {fmtBytes(phase.total)} ({pct} %)</span>
          </p>
          <div className="flex items-center gap-2">
            <progress aria-label="Upload progress" max={phase.total || 1} value={phase.total === 0 ? 1 : phase.sent} className="h-2 min-w-0 flex-1" />
            <Button size="sm" variant="outline" data-testid="drop-cancel" onClick={onCancel}>
              Cancel
            </Button>
          </div>
        </div>
      )
    }
    case 'sealing':
      return (
        <p role="status" data-testid="drop-sealing" className="[overflow-wrap:anywhere]">
          Sealing and sending {name}… (this Mac&apos;s host encrypts it and sends it to the relay)
        </p>
      )
    case 'done':
      return (
        <p role="status" data-testid="drop-done" className="text-success [overflow-wrap:anywhere]">
          Sent {name}.
        </p>
      )
    case 'cancelled':
      return (
        <p role="status" data-testid="drop-cancelled" className="text-text-muted [overflow-wrap:anywhere]">
          Upload of {name} cancelled. Nothing was sent.
        </p>
      )
    case 'error':
      return (
        <DropErrorLine detail={phase.detail}>
          <p className="text-text-muted [overflow-wrap:anywhere]">Not sent: {name}</p>
          {phase.detail.code === 'drop.version_conflict' && conflict && (
            <Button size="sm" variant="outline" onClick={conflict}>
              Reload
            </Button>
          )}
        </DropErrorLine>
      )
  }
}
