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
import { suggestedWhere } from '@/api/addOnSwitches'
import { queries } from '@/api/queries'
import { workdirQueries } from '@/api/workdir'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import {
  DAYS,
  adminCommand,
  destinationLine,
  VIEWS,
  fileBase64,
  LIMITS,
  fmtSize,
  reachNote,
  sameOrigin,
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

/** Publish as link: how long the one-file link works, sent with the prepare call and the fallback of the sheet's lead when the staged copy does not say. */
const PUBLISH_DAYS = 7

export const refreshShares = (qc: ReturnType<typeof useQueryClient>, ws: string) => qc.invalidateQueries({ queryKey: ['shares', ws] })

type Stage =
  | { at: 'form' }
  | { at: 'one' }
  | { at: 'confirm'; staged: Staged }
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
function Where({ server, share, from }: { server: string | null; share: string | null; from: 'computer' | 'hub' | null }) {
  const note = reachNote(server, share, from)
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
      {note && <span className="mt-1 block">{note}</span>}
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
  // Where the files go is told by the Publish add-on row (the same facts TurnOnPublish reads): from a hub, or from the
  // Publish server on this computer. Its suggestion only counts for the address that is actually configured.
  const addOns = useQuery(queries.addOns(ws))
  const row = addOns.data?.available ? addOns.data.add_ons.find((r) => r.id === 'publish') : undefined
  // A row without a suggestion says nothing about this address: it never counts as a match.
  const suggestedServer = suggestedWhere(row)?.server
  const sameAddress = !!suggestedServer && !!server && sameOrigin(suggestedServer, server)
  const from = sameAddress ? row?.suggest_from ?? null : null
  const hubName = sameAddress ? row?.suggest_hub ?? null : null
  // Block only while the row is really loading the first time. A query that is switched off or failed leaves the row
  // unknown: the host-name line shows and Sign is not held back.
  const rowLoading = addOns.isPending && addOns.fetchStatus !== 'idle'
  // No line, and no Sign, until the server is known and the row has answered: never a guess.
  const where = rowLoading ? null : destinationLine(from, hubName, server)
  // "this computer" only when the row says the source is this computer
  const reach = reachNote(server, share, from)

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
  // the signature settled: closing the review after that must not discard the copy that is being sent
  const signed = useRef(false)

  // P03: the ticket's working files (the folder the Working files card sums up), offered next to the results
  const folder = useQuery({ ...workdirQueries.ofTicket(ws, ticket.key), enabled: stage.at === 'form' })
  const [workPicked, setWorkPicked] = useState<string[]>([])
  const resultNames = ticket.artifacts.map((a) => a.name.toLowerCase())
  const work = (folder.data?.available && folder.data.exists ? (folder.data.items ?? []) : []).filter((f) => !resultNames.includes(f.name.toLowerCase()))
  const workRows = work.filter((f) => unshareable({ name: f.name, kind: 'file', bytes: f.bytes }) === null)
  // empty files are not offered; one line says so, instead of a greyed-out row with no way forward
  const emptyNames = [...ticket.artifacts.filter((a) => a.bytes === 0 && unshareable(a) !== null).map((a) => a.name), ...work.filter((f) => f.bytes === 0).map((f) => f.name)]
  const files = ticket.artifacts.filter((a) => a.bytes !== 0).map((a) => ({ a, why: unshareable(a) }))
  // what is still offered AND ticked: a name that left the list (reload, or a result of that name arrived) is dropped here
  const workSel = workPicked.filter((n) => workRows.some((f) => f.name === n))
  const total =
    ticket.artifacts.filter((a) => picked.includes(a.name)).reduce((n, a) => n + (a.bytes ?? 0), 0) +
    work.filter((f) => workSel.includes(f.name)).reduce((n, f) => n + f.bytes, 0) +
    added.reduce((n, f) => n + f.size, 0)
  const count = picked.length + workSel.length + added.length

  const addFiles = async (list: FileList | null) => {
    const chosen = Array.from(list ?? [])
    if (pick.current) pick.current.value = ''
    if (chosen.length === 0 || adding) return
    setAdding(true)
    setError('')
    const taken = [...ticket.artifacts.map((a) => a.name), ...work.map((f) => f.name), ...added.map((f) => f.name)]
    let n = count
    try {
      for (const file of chosen) {
        if (n >= LIMITS.files) {
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
        n += 1
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
      const got = await sharesApi.prepare(ws, { ticket: ticket.key, artifacts: picked, ...(workSel.length ? { workfiles: workSel } : {}), ...(added.length ? { uploads: added.map((f) => f.upload_id) } : {}), kind, days, ...(kind === 'once' ? { views } : {}) }, prepareIdem)
      if (!alive.current) return discard(got.staged)
      void refreshShares(qc, ws)
      setStage({ at: 'confirm', staged: got.staged })
    } catch (e) {
      if (alive.current) setError(shareError(e))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  // Publish as link: one file, "Anyone with the link", 7 days. One press prepares the copy and goes straight to the
  // signature (its review names the file, the server and the link kind); nothing leaves until it is signed.
  const publishOne = async () => {
    if (busy || !only) return
    setBusy(true)
    setError('')
    try {
      const got = await sharesApi.prepare(ws, { ticket: ticket.key, artifacts: [only], kind: 'secret', days: PUBLISH_DAYS }, prepareIdem)
      if (!alive.current) return discard(got.staged)
      void refreshShares(qc, ws)
      setStage({ at: 'confirm', staged: got.staged })
    } catch (e) {
      if (alive.current) setError(shareError(e))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  // Publish as link: the press that opened this flow already said what happens, so the copy is prepared as soon as the
  // server is known to be set up. Prepare only: the sign sheet that follows is never armed, Sign stays the person's press.
  const autoPrepared = useRef(false)
  // The flow closed or unmounted while a copy was being prepared: that copy is discarded when it arrives, never shown or signed.
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  useEffect(() => {
    if (!only || stage.at !== 'one' || autoPrepared.current || info.isPending) return
    if (data && !data.configured) return
    autoPrepared.current = true
    void publishOne()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [only, stage.at, info.isPending, data])

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

  // ---- one file: Publish as link. No dialog of its own: the copy is prepared at once (aside, nothing leaves the computer)
  // and the ONE sign sheet follows. Only a failure (or "no publish server") shows a small panel, before anything is signed.
  if (stage.at === 'one' && only) {
    const noServer = !!data && !data.configured
    return (
      <Dialog open onOpenChange={(o) => !o && onClose()}>
        <DialogContent className="max-w-md gap-3 border-border bg-surface">
          <DialogHeader>
            <DialogTitle>Publish {only} as a link?</DialogTitle>
            <DialogDescription>{error || noServer ? 'Nothing was sent.' : 'Getting a copy ready. Nothing leaves this computer yet.'}</DialogDescription>
          </DialogHeader>
          <div className="space-y-2 text-[13px]">
            {noServer && <p className="text-warning">No publish server is set up yet. Set one up on the Add-ons page, then come back.</p>}
            {error && <Failure text={error} />}
            {busy && !error && (
              <p role="status" className="flex items-center gap-2 text-text-muted">
                <Loader2 className="size-4 animate-spin" aria-hidden /> Preparing…
              </p>
            )}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            {error && !noServer && <Button onClick={() => void publishOne()}>Try again</Button>}
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
              {files.length === 0 && workRows.length === 0 && added.length === 0 && (
                <p className="text-text-muted">{emptyNames.length > 0 ? 'Nothing is ready to share yet. Add a file from this computer.' : 'This ticket has no files yet. Add one from this computer.'}</p>
              )}
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
              {workRows.map((f) => (
                <label key={`work:${f.name}`} className="flex items-start gap-2" data-workfile={f.name}>
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={workSel.includes(f.name)}
                    onChange={(e) => setWorkPicked((p) => (e.target.checked ? [...p, f.name] : p.filter((n) => n !== f.name)))}
                  />
                  <span className="min-w-0 flex-1">
                    <Inline>{f.name}</Inline>
                    <span className="text-text-faint"> · {fmtSize(f.bytes)} · working file</span>
                  </span>
                </label>
              ))}
              {emptyNames.map((n) => (
                <p key={`empty:${n}`} className="text-[12px] text-text-muted">
                  {n} is empty, so it is left out.
                </p>
              ))}
              {folder.data?.items_truncated && <p className="text-[12px] text-text-muted">More files than shown.</p>}
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
            <Where server={server} share={share} from={from} />
            {error && <Failure text={error} />}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            {count === 0 && !busy && <span className="mr-auto self-center text-[12px] text-text-muted">Pick a file first.</span>}
            <Button onClick={() => void prepare()} disabled={busy || adding || count === 0 || (!!data && !data.configured)}>
              {busy ? 'Preparing…' : 'Review selected files'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  // ---- review and sign in ONE sheet (r12-P11): the files and where they go are the lead, the host's review loads at once
  // (prepare only: nothing is armed and nothing signs by itself, Sign is the person's own press), and "Share files" signs.
  if (stage.at === 'confirm') {
    const s = stage.staged
    // the one-file wording (title, button and lead) only when the staged copy is exactly the file that was named
    const one = !!only && s.files.length === 1 && s.files[0].name === only
    const lasts = s.days ?? PUBLISH_DAYS
    return (
      <SignPopover
        key={`${s.staged_id}:${where ? 'known' : 'unknown'}`}
        title={one ? `Publish ${only} as a link?` : `Share files of ${s.ticket ?? ticket.key}`}
        ws={ws}
        // where the files go is not known: nothing is prepared and Sign stays off
        action={where ? { action: 'publish_share', key: s.ticket ?? ticket.key, staged_id: s.staged_id } : null}
        hint={where ? null : rowLoading || info.isPending ? 'Checking where the files go…' : 'Where the files go is not known, so nothing can be signed.'}
        anchor={null}
        prepareOnOpen
        autoStart={false}
        confirmLabel={one ? 'Publish' : 'Share files'}
        lead={
          one ? (
            <div className="space-y-2 text-[13px]">
              <ul aria-label="Files to share" className="divide-y divide-border rounded border border-border px-2">
                {s.files.map((f) => (
                  <li key={f.name} className="py-1.5">
                    <Inline>{f.name}</Inline> <span className="text-text-faint">· {fmtSize(f.size)}</span>
                    <span className="block text-[12px] text-text-muted">works for {lasts} day{lasts === 1 ? '' : 's'}</span>
                  </li>
                ))}
              </ul>
              {where && <p data-testid="where-files-go" className="font-medium">{where}</p>}
              {reach && <p className="text-[12px] text-text-muted">{reach}</p>}
              <p role="note" className="text-[12px] text-warning">Anyone with the link can read this file. A copy cannot be taken back. You see the link once.</p>
              <InfoDrawer text="Details">
                {server && <p className="break-all font-mono">Files are sent to {server}</p>}
                {share && <p className="break-all font-mono">The link opens at {share}</p>}
                <p className="mt-1 break-all font-mono">Prepared copy {s.staged_id}</p>
                {s.files.map((f) => (
                  <p key={f.name} className="break-all font-mono">
                    {f.name}: sha256 {f.sha256}
                  </p>
                ))}
              </InfoDrawer>
            </div>
          ) : (
          <div className="space-y-2 text-[13px]">
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
            {where && <p data-testid="where-files-go" className="font-medium">{where}</p>}
            {reach && <p className="text-[12px] text-text-muted">{reach}</p>}
            <p role="note" className="text-[12px] text-warning">Anyone with the link can read these files. A copy cannot be taken back.</p>
            <InfoDrawer>
              {server && <p className="break-all font-mono">Files are sent to {server}</p>}
              {share && <p className="break-all font-mono">The link opens at {share}</p>}
              <p className="mt-1 break-all font-mono">Prepared copy {s.staged_id}</p>
              {s.files.map((f) => (
                <p key={f.name} className="break-all font-mono">
                  {f.name}: sha256 {f.sha256}
                </p>
              ))}
            </InfoDrawer>
          </div>
          )
        }
        onSettled={(phase) => {
          if (phase === 'done') {
            signed.current = true
            void send(s)
          }
        }}
        onClose={() => {
          if (signed.current) return
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
  return stage.at === 'link' ? <LinkOnce sent={stage.sent} onDone={onClose} /> : null
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
