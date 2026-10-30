// Share files of a ticket with a link: pick files, prepare a copy aside (nothing leaves the computer), sign ONE review
// in the existing signing flow, send, then show the link ONCE.
//
// THE LINK RULE. The link lives only in this component's state (`sent`), from the answer of `sharesApi.send` until the
// person presses Done (or the page unloads). It is not written to localStorage/sessionStorage, not put in a query or
// mutation cache (`send` is a plain call, never `useMutation`), not logged, not in a toast, not in an error text. The
// dialog cannot be dismissed by Escape or a click outside: only "Done" ends it, so one stray key cannot lose a link.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Link } from '@tanstack/react-router'
import { Check, Copy, Loader2, Plus } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { copyText } from '@/app/copyLink'
import { SignPopover } from '@/components/sign/SignPopover'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import {
  DAYS,
  VIEWS,
  adminCommand,
  fileBase64,
  LIMITS,
  fmtSize,
  reachNote,
  isUnknownWorkspace,
  isUnreachable,
  unpickable,
  kindText,
  newIdem,
  shareQueries,
  sharesApi,
  unshareable,
  type Days,
  type Picked,
  type Sent,
  type ShareKind,
  type Staged,
  type Views,
} from '@/api/shares'
import type { Artifact } from '@/api/types'
import { fmtDateTime } from '@/lib/time'
import { publishError as shareError } from './hubErrors'

export const refreshShares = (qc: ReturnType<typeof useQueryClient>, ws: string) => qc.invalidateQueries({ queryKey: ['shares', ws] })

type Stage =
  | { at: 'form' }
  | { at: 'one' }
  | { at: 'confirm'; staged: Staged }
  | { at: 'sign'; staged: Staged }
  | { at: 'send'; staged: Staged; connected?: boolean; error?: { text: string; unknown: boolean; unreachable: boolean } }
  | { at: 'link'; sent: Sent }

function Failure({ text }: { text: string }) {
  return (
    <p role="alert" className="text-[13px] text-danger">
      {text}
    </p>
  )
}

/** Where the files go and where the link opens, in the dialog's words. */
function Where({ server, share }: { server: string | null; share: string | null }) {
  if (!server) return null
  return (
    <p className="text-[12px] text-text-muted">
      The files are sent to <Inline>{server}</Inline>
      {share ? (
        <>
          {' '}
          and the link opens at <Inline>{share}</Inline>
        </>
      ) : null}
      .
      {reachNote(server, share) && <span className="mt-1 block">{reachNote(server, share)}</span>}
    </p>
  )
}

/**
 * The whole flow for one ticket. `resume` starts at the confirm step for a copy that is already prepared (an agent
 * prepared it, or an earlier attempt stopped before the signature).
 */
export function ShareFlow({ ws, ticket, resume, only, onClose }: { ws: string; ticket: { key: string; artifacts: Artifact[] }; resume?: Staged; /** Publish as link (run-14 P01): the one file named, a small panel instead of the form, then Sign and publish. */ only?: string; onClose: () => void }) {
  const qc = useQueryClient()
  const [stage, setStage] = useState<Stage>(resume ? { at: 'confirm', staged: resume } : only ? { at: 'one' } : { at: 'form' })
  const info = useQuery(shareQueries.ofTicket(ws, ticket.key))
  const data = info.data && info.data.available ? info.data : null
  const server = data?.server ?? null
  const share = data?.share ?? null

  // form
  const [picked, setPicked] = useState<string[]>([])
  // files picked on this computer (P05): kept aside by the host, named again in Review, covered by the one signature
  const [added, setAdded] = useState<Picked[]>([])
  const [adding, setAdding] = useState(false)
  const pick = useRef<HTMLInputElement>(null)
  const [kind, setKind] = useState<ShareKind>('secret')
  const [days, setDays] = useState<Days>(7)
  const [views, setViews] = useState<Views>(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [prepareIdem] = useState(newIdem)
  // one idem per staged copy for send, kept for every retry of it
  const sendIdem = useRef<{ id: string; idem: string } | null>(null)
  const sending = useRef(false)

  const files = ticket.artifacts.map((a) => ({ a, why: unshareable(a) }))
  const total = ticket.artifacts.filter((a) => picked.includes(a.name)).reduce((n, a) => n + (a.bytes ?? 0), 0) + added.reduce((n, f) => n + f.size, 0)
  const count = picked.length + added.length

  const addFiles = async (list: FileList | null) => {
    const chosen = Array.from(list ?? [])
    if (pick.current) pick.current.value = ''
    if (chosen.length === 0 || adding) return
    setAdding(true)
    setError('')
    const taken = [...ticket.artifacts.map((a) => a.name), ...added.map((f) => f.name)]
    try {
      for (const file of chosen) {
        if (picked.length + taken.length - ticket.artifacts.length >= LIMITS.files) {
          setError(`A share holds at most ${LIMITS.files} files.`)
          break
        }
        const why = unpickable(file, taken)
        if (why) {
          setError(`${file.name}: ${why}`)
          continue
        }
        const got = await sharesApi.upload(ws, { name: file.name, data: await fileBase64(file) }, newIdem())
        taken.push(got.name)
        setAdded((a) => [...a, got])
      }
    } catch (e) {
      setError(shareError(e))
    } finally {
      setAdding(false)
    }
  }

  const discard = (staged: Staged) => {
    // best effort: a person who closes before the signature does not want this copy to wait
    void sharesApi
      .discard(ws, staged.staged_id, newIdem())
      .catch(() => undefined)
      .finally(() => void refreshShares(qc, ws))
  }

  const prepare = async () => {
    if (busy || count === 0) return
    setBusy(true)
    setError('')
    try {
      const got = await sharesApi.prepare(ws, { ticket: ticket.key, artifacts: picked, ...(added.length ? { uploads: added.map((f) => f.upload_id) } : {}), kind, days, ...(kind === 'once' ? { views } : {}) }, prepareIdem)
      void refreshShares(qc, ws)
      setStage({ at: 'confirm', staged: got.staged })
    } catch (e) {
      setError(shareError(e))
    } finally {
      setBusy(false)
    }
  }

  // Publish as link: one file, "Anyone with the link", 7 days. One press prepares the copy and goes straight to the
  // signature (its review names the file, the server and the link kind); nothing leaves until it is signed.
  const publishOne = async () => {
    if (busy || !only) return
    setBusy(true)
    setError('')
    try {
      const got = await sharesApi.prepare(ws, { ticket: ticket.key, artifacts: [only], kind: 'secret', days: 7 }, prepareIdem)
      void refreshShares(qc, ws)
      setStage({ at: 'sign', staged: got.staged })
    } catch (e) {
      setError(shareError(e))
    } finally {
      setBusy(false)
    }
  }

  const send = async (staged: Staged) => {
    if (sending.current) return
    sending.current = true
    if (sendIdem.current?.id !== staged.staged_id) sendIdem.current = { id: staged.staged_id, idem: newIdem() }
    setStage({ at: 'send', staged })
    const idem = sendIdem.current.idem
    const fail = (e: unknown, connected?: boolean) => {
      void refreshShares(qc, ws)
      setStage({ at: 'send', staged, ...(connected ? { connected } : {}), error: { text: shareError(e), unknown: isUnknownWorkspace(e), unreachable: isUnreachable(e) } })
    }
    try {
      let sent: Sent
      try {
        sent = await sharesApi.send(ws, staged.staged_id, idem)
      } catch (e) {
        if (!isUnknownWorkspace(e)) throw e
        // The sharing server does not know this workspace yet: connect it by itself and send once more.
        setStage({ at: 'send', staged, connected: true })
        try {
          await sharesApi.register(ws, newIdem())
        } catch {
          throw e // not allowed here (or not the owner): the first refusal, in its own words
        }
        try {
          sent = await sharesApi.send(ws, staged.staged_id, idem)
        } catch (e2) {
          return fail(e2, true)
        }
      }
      void refreshShares(qc, ws)
      setStage({ at: 'link', sent })
    } catch (e) {
      fail(e)
    } finally {
      sending.current = false
    }
  }

  // ---- one file: Publish as link
  if (stage.at === 'one' && only) {
    const file = ticket.artifacts.find((a) => a.name === only)
    return (
      <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
        <DialogContent className="max-w-md gap-3 border-border bg-surface">
          <DialogHeader>
            <DialogTitle>Publish {only} as a link?</DialogTitle>
            <DialogDescription>Anyone with the link can open this file. Only this file leaves your computer. You see the link once, so copy it right away.</DialogDescription>
          </DialogHeader>
          <div className="space-y-2 text-[13px]">
            {file?.bytes != null && <p className="text-text-muted">{fmtSize(file.bytes)} · works for 7 days</p>}
            {data && !data.configured && <p className="text-warning">No publish server is set up yet. Set one up on the Add-ons page, then come back.</p>}
            <Where server={server} share={share} />
            {error && <Failure text={error} />}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={() => void publishOne()} disabled={busy || (!!data && !data.configured)}>
              {busy ? 'Preparing…' : 'Sign and publish'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  // ---- form
  if (stage.at === 'form') {
    return (
      <Dialog open onOpenChange={(o) => !o && !busy && onClose()}>
        <DialogContent className="max-h-[90vh] max-w-md gap-3 overflow-y-auto border-border bg-surface">
          <DialogHeader>
            <DialogTitle>Share files</DialogTitle>
            <DialogDescription>
              Pick files for <Inline>{ticket.key}</Inline>. Nothing is sent until you approve.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-[13px]">
            {data && !data.configured && <p className="text-warning">No publish server is set up yet. Set one up on the Add-ons page, then come back.</p>}
            <fieldset className="space-y-1.5">
              <legend className="mb-1 font-medium">Files</legend>
              {files.length === 0 && added.length === 0 && <p className="text-text-muted">This ticket has no files yet. Add one from this computer.</p>}
              {files.map(({ a, why }) => (
                <label key={a.name} className="flex items-start gap-2">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    disabled={!!why}
                    checked={picked.includes(a.name)}
                    onChange={(e) => setPicked((p) => (e.target.checked ? [...p, a.name] : p.filter((n) => n !== a.name)))}
                  />
                  <span className="min-w-0 flex-1">
                    <Inline>{a.name}</Inline>
                    {a.bytes !== null && <span className="text-text-faint"> · {fmtSize(a.bytes)}</span>}
                    {why && <span className="block text-[12px] text-text-muted">{why}</span>}
                  </span>
                </label>
              ))}
              {added.map((f) => (
                <div key={f.upload_id} className="flex items-start gap-2" data-picked={f.upload_id}>
                  <input type="checkbox" className="mt-0.5" checked readOnly aria-label={`${f.name} from this computer`} />
                  <span className="min-w-0 flex-1">
                    <Inline>{f.name}</Inline>
                    <span className="text-text-faint"> · {fmtSize(f.size)}</span>
                  </span>
                  <Button type="button" size="sm" variant="ghost" className="h-6 px-2 text-[12px]" aria-label={`Remove ${f.name}`} onClick={() => setAdded((a) => a.filter((x) => x.upload_id !== f.upload_id))}>
                    Remove
                  </Button>
                </div>
              ))}
              <div className="flex items-center gap-2 pt-1">
                <input ref={pick} type="file" multiple hidden data-testid="share-file-input" onChange={(e) => void addFiles(e.target.files)} />
                <Button type="button" size="sm" variant="secondary" disabled={adding || busy} onClick={() => pick.current?.click()}>
                  {adding ? <Loader2 className="mr-1 size-3.5 animate-spin" aria-hidden /> : <Plus className="mr-1 size-3.5" aria-hidden />}
                  Add a file
                </Button>
                <span className="text-[12px] text-text-muted">from this computer, up to 1 MB</span>
              </div>
              {count > 0 && <p className="text-[12px] text-text-muted">{count} selected{total ? `, ${fmtSize(total)}` : ''}.</p>}
            </fieldset>
            <fieldset className="space-y-1.5">
              <legend className="mb-1 font-medium">Kind of link</legend>
              <label className="flex items-start gap-2">
                <input type="radio" name="share-kind" checked={kind === 'secret'} onChange={() => setKind('secret')} className="mt-0.5" />
                <span>
                  Anyone with the link
                  <span className="block text-[12px] text-text-muted">Anyone who has the link can open the files until it ends or you stop sharing.</span>
                </span>
              </label>
              <label className="flex items-start gap-2">
                <input type="radio" name="share-kind" checked={kind === 'once'} onChange={() => setKind('once')} className="mt-0.5" />
                <span>
                  Limited-view link
                  <span className="block text-[12px] text-text-muted">Anyone with the link can open the files, but only a few times. Then it stops.</span>
                </span>
              </label>
              {kind === 'once' && (
                <div className="flex items-center gap-2 pl-6">
                  <Label htmlFor="share-views">Up to</Label>
                  <select id="share-views" className="h-8 rounded border border-border bg-bg px-2" value={views} onChange={(e) => setViews(Number(e.target.value) as Views)}>
                    {VIEWS.map((v) => (
                      <option key={v} value={v}>
                        {v} view{v === 1 ? '' : 's'}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </fieldset>
            <div className="flex items-center gap-2">
              <Label htmlFor="share-days">Works for</Label>
              <select id="share-days" className="h-8 rounded border border-border bg-bg px-2" value={days} onChange={(e) => setDays(Number(e.target.value) as Days)}>
                {DAYS.map((d) => (
                  <option key={d} value={d}>
                    {d} day{d === 1 ? '' : 's'}
                  </option>
                ))}
              </select>
            </div>
            <Where server={server} share={share} />
            {error && <Failure text={error} />}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={() => void prepare()} disabled={busy || adding || count === 0 || (!!data && !data.configured)}>
              {busy ? 'Preparing…' : 'Review selected files'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  // ---- confirm: every file with its size and a link to where it is on the ticket, before the signature
  if (stage.at === 'confirm') {
    const s = stage.staged
    const sum = s.files.reduce((n, f) => n + f.size, 0)
    return (
      <Dialog
        open
        onOpenChange={(o) => {
          if (o) return
          discard(s)
          onClose()
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-md gap-3 overflow-y-auto border-border bg-surface">
          <DialogHeader>
            <DialogTitle>Review your share</DialogTitle>
            <DialogDescription>
              {s.origin === 'agent' ? 'An agent prepared this copy. ' : ''}Files are uploaded only after you approve.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 text-[13px]">
            <p>
              {kindText(s.kind, s.views)} · works for {s.days} day{s.days === 1 ? '' : 's'}
            </p>
            <ul aria-label="Files to share" className="divide-y divide-border rounded border border-border px-2">
              {s.files.map((f) => (
                <li key={f.name} className="flex flex-wrap items-baseline gap-x-2 py-1.5">
                  <Inline>{f.name}</Inline>
                  <span className="text-text-faint">{fmtSize(f.size)}</span>
                  <Link to="/ticket/$key" params={{ key: s.ticket ?? ticket.key }} search={{ tab: 'artifacts' }} target="_blank" rel="noreferrer" className="ml-auto text-[12px] text-brand hover:underline" aria-label={`Open ${f.name} on the ticket`}>
                    Open on the ticket
                  </Link>
                </li>
              ))}
            </ul>
            <p className="text-[12px] text-text-muted">
              {s.files.length} file{s.files.length === 1 ? '' : 's'}, {fmtSize(sum)} in all.
            </p>
            <Where server={server} share={share} />
            <InfoDrawer>
              <p className="mt-1 break-all font-mono">Prepared copy {s.staged_id}</p>
              {s.files.map((f) => (
                <p key={f.name} className="break-all font-mono">
                  {f.name}: sha256 {f.sha256}
                </p>
              ))}
            </InfoDrawer>
          </div>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => {
                discard(s)
                onClose()
              }}
            >
              Cancel
            </Button>
            <Button onClick={() => setStage({ at: 'sign', staged: s })}>Continue to approval</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  // ---- sign: the existing signing flow, one review
  if (stage.at === 'sign') {
    const s = stage.staged
    return (
      <SignPopover
        title={`Share files of ${s.ticket ?? ticket.key}`}
        ws={ws}
        action={{ action: 'publish_share', key: s.ticket ?? ticket.key, staged_id: s.staged_id }}
        onSettled={(phase, _state, _request) => {
          if (phase === 'done') void send(s)
        }}
        onClose={() => {
          discard(s)
          onClose()
        }}
      />
    )
  }

  // ---- send
  if (stage.at === 'send') {
    const s = stage.staged
    const err = stage.error
    return (
      <Dialog open onOpenChange={(o) => !o && !!err && onClose()}>
        <DialogContent className="max-w-md gap-3 border-border bg-surface" onInteractOutside={(e) => e.preventDefault()} onEscapeKeyDown={(e) => !err && e.preventDefault()}>
          <DialogHeader>
            <DialogTitle>{err ? 'The files were not sent' : 'Sending the files…'}</DialogTitle>
            <DialogDescription>{err ? 'Nothing is shared yet. Your signature stays good for an hour.' : 'This takes a moment. Do not close this window.'}</DialogDescription>
          </DialogHeader>
          {!err && (
            <ul role="status" className="space-y-1 text-[13px]">
              <li className="text-success">✓ Signed</li>
              {stage.connected && <li className="text-success">✓ This workspace is now connected to sharing</li>}
              <li className="flex items-center gap-2 text-text-muted">
                <Loader2 className="size-4 animate-spin" aria-hidden /> Sending {s.files.length} file{s.files.length === 1 ? '' : 's'}…
              </li>
            </ul>
          )}
          {err && <Failure text={err.text} />}
          {err?.unreachable && <p className="text-[12px] text-text-muted">Your signature stays good for an hour. Press Try again when sharing is back.</p>}
          {err?.unknown && data && (
            <InfoDrawer>
              <p className="mt-1">The server’s admin runs:</p>
              <p className="break-all font-mono">{adminCommand(data.workspace, data.owner)}</p>
            </InfoDrawer>
          )}
          {err && (
            <DialogFooter>
              <Button variant="ghost" onClick={onClose}>
                Close
              </Button>
              <Button onClick={() => void send(s)}>Try again</Button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>
    )
  }

  // ---- link: shown once
  return <LinkOnce sent={stage.sent} onDone={onClose} />
}

/** The link, once. Only "Done" ends it. Nothing here writes the link anywhere but the screen and the clipboard. */
export function LinkOnce({ sent, onDone }: { sent: Sent; onDone: () => void }) {
  const [copied, setCopied] = useState<'no' | 'yes' | 'failed'>('no')
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => {
    field.current?.select()
  }, [])
  const copy = async () => {
    const ok = await copyText(sent.link)
    setCopied(ok ? 'yes' : 'failed')
    if (!ok) field.current?.select()
  }
  return (
    <Dialog open>
      <DialogContent
        className="max-w-lg gap-3 border-border bg-surface"
        showCloseButton={false}
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
        aria-describedby="link-once-note"
      >
        <DialogHeader>
          <DialogTitle>Your link</DialogTitle>
          <DialogDescription id="link-once-note">
            {kindText(sent.kind, sent.view_limit)}
            {sent.expires_at ? `, works until ${fmtDateTime(sent.expires_at)}` : ''}.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2 text-[13px]">
          <Label htmlFor="share-link">Link</Label>
          <div className="flex gap-2">
            <input id="share-link" ref={field} readOnly value={sent.link} className="h-8 min-w-0 flex-1 rounded border border-border bg-bg px-2 font-mono text-[12px]" onFocus={(e) => e.currentTarget.select()} />
            <Button size="sm" variant="secondary" onClick={() => void copy()}>
              {copied === 'yes' ? <Check className="mr-1 size-3.5" aria-hidden /> : <Copy className="mr-1 size-3.5" aria-hidden />}
              {copied === 'yes' ? 'Copied' : 'Copy'}
            </Button>
          </div>
          <p role="status" className="min-h-5 text-[12px]" data-testid="copy-status">
            {copied === 'yes' && 'Link copied to the clipboard.'}
            {copied === 'failed' && 'Could not copy automatically. The link is selected: copy it by hand (Ctrl or Cmd + C).'}
          </p>
          <p role="note" className="font-medium text-warning">
            You can copy this link only now.
          </p>
          <p className="text-text-muted">orch keeps no copy. If you lose it, stop sharing on the Shares page and share again.</p>
        </div>
        <DialogFooter>
          <Button onClick={onDone}>I’ve saved the link — close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
