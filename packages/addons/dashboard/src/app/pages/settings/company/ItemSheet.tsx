import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { isChanged, orgQueries, plain, STATUS_WORD, type OrgKind, type OrgServerItem, type OrgShown, type OrgTool } from '@/api/org'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { Switch } from '@/components/ui/switch'
import { Action } from '../hub/shared'

// The details sheet of one company skill or server. Plain words, plain text: every string below came from the company hub
// and is drawn as a React text node (no markdown, no HTML), so `**bold**` stays `**bold**` and a tag stays a tag.

/** The limits the owner is told before turning something on. A point the host's own notes already make is not said twice. */
const LIMITS: Record<OrgKind, { when?: (s: OrgShown) => boolean; match: RegExp; text: string }[]> = {
  skill: [
    { match: /ranks below|cannot widen|override/i, text: "A skill is advice. It cannot override orch's rules or give an agent more access." },
    { match: /already read|stays read/i, text: 'Turning a skill off does not take back what a run has already read.' },
  ],
  mcp: [
    { match: /untrusted/i, text: 'What a tool answers is text from your company server. orch does not check it.' },
    { when: (s) => (s.item as OrgServerItem).auth === 'none', match: /no sign-in|only hides/i, text: 'This server has no sign-in. Turning a tool off only hides it from the agent; anyone who can reach the server can still use it.' },
    { when: (s) => (s.item as OrgServerItem).auth === 'hub_secret', match: /separate agent user/i, text: 'A server with a company secret works only when agents run as a separate agent user. On this computer without that, it stays off.' },
    { match: /already read|stays read/i, text: 'Turning a server off does not take back what a run has already read.' },
  ],
}

function limitLines(s: OrgShown): string[] {
  const own = LIMITS[s.kind].filter((l) => (l.when ? l.when(s) : true) && !(s.needs_isolation && l.match.test('separate agent user')) && !s.notes.some((n) => l.match.test(n))).map((l) => l.text)
  return [...s.notes.map(plain), ...own]
}

function refusal(e: unknown): string {
  const code = e instanceof ApiError ? e.code : ''
  if (code === 'role.denied') return 'Only the workspace owner can do this.'
  if (code === 'op.not_allowed' || code === 'session.readonly' || code === 'step_up_required') return 'Sign in on this computer to turn this on.'
  if (code === 'not_found') return 'The hub no longer offers this. Get the latest and look again.'
  if (e instanceof Error && /needs_isolation/.test(e.message)) return 'This needs a separate agent user, so it stays off on this computer.'
  return 'That did not work. Try again.'
}

function ToolRow({ tool, enabled, busy, onSet }: { tool: OrgTool; enabled: boolean; busy: boolean; onSet: (on: boolean) => void }) {
  const tooLong = tool.status === 'too_long'
  return <li className="flex items-start gap-3 py-2" data-testid={`company-tool-${tool.name}`}>
    <div className="min-w-0 flex-1 space-y-1">
      <p className="font-medium">{plain(tool.name)}{tool.title ? <span className="ml-2 font-normal text-text-muted" data-testid={`company-tool-title-${tool.name}`}>{plain(tool.title)}</span> : null}</p>
      {tooLong
        ? <p className="text-warning" data-testid={`company-tool-toolong-${tool.name}`}>Too long to review — stays off</p>
        : <>
          {tool.description && <p className="whitespace-pre-wrap text-text-muted" data-testid={`company-tool-desc-${tool.name}`}>{plain(tool.description)}</p>}
          {tool.input_schema && <details><summary className="cursor-pointer text-xs text-text-muted">Details: what it accepts</summary>
            <pre className="mt-1 max-h-48 overflow-auto rounded-md border border-border bg-surface-2 p-2 text-xs whitespace-pre-wrap break-words" data-testid={`company-tool-schema-${tool.name}`}>{plain(tool.input_schema)}</pre></details>}
          {tool.status === 'changed' && <p className="text-warning" data-testid={`company-tool-changed-${tool.name}`}>Changed — turn on again to allow</p>}
        </>}
    </div>
    {!tooLong && <>
      <span className="text-xs text-text-muted">{tool.status === 'on' ? 'On' : 'Off'}</span>
      <Switch aria-label={`${tool.name} on or off`} data-testid={`company-tool-switch-${tool.name}`} checked={tool.status === 'on'} disabled={!enabled || busy} onCheckedChange={onSet} />
    </>}
  </li>
}

function ServerDetails({ item, tools, serverOn, busy, onTool }: { item: OrgServerItem; tools: OrgTool[]; serverOn: boolean; busy: boolean; onTool: (t: OrgTool, on: boolean) => void }) {
  return <div className="space-y-3">
    <dl className="space-y-1">
      <div><dt className="inline font-medium">Address: </dt><dd className="inline break-all" data-testid="company-url">{plain(item.url) || 'not shown'}</dd></div>
      <div><dt className="inline font-medium">Sign-in: </dt><dd className="inline" data-testid="company-auth">{item.auth === 'none' ? 'No sign-in' : `Company secret: ${plain(item.secret) || 'not shown'}`}</dd></div>
      {item.description && <div><dt className="inline font-medium">What it is for: </dt><dd className="inline">{plain(item.description)}</dd></div>}
    </dl>
    <div>
      <h3 className="font-medium">Tools</h3>
      {tools.length === 0
        ? <p className="text-text-muted">No tools seen yet. Turn the server on, then look again.</p>
        : <>
          {!serverOn && <p className="text-text-muted">Turn the server on first to allow its tools. Every tool starts off.</p>}
          <ul className="divide-y divide-border">{tools.map((t) => <ToolRow key={t.name} tool={t} enabled={serverOn} busy={busy} onSet={(on) => onTool(t, on)} />)}</ul>
        </>}
    </div>
  </div>
}

export function ItemSheet({ ws, kind, name, onClose }: { ws: string; kind: OrgKind; name: string; onClose: () => void }) {
  const qc = useQueryClient()
  const shown = useQuery(orgQueries.item(ws, kind, name))
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const done = () => { void qc.invalidateQueries({ queryKey: ['org', ws] }) }
  const s = shown.data
  const digest = s?.item.digest ?? null

  const turnOn = () => {
    if (!digest) return
    setBusy(true)
    setMessage('')
    api.acceptOrgItem(ws, kind, name, digest).then(
      () => { done(); onClose() },
      async (e) => {
        // The digest the host holds is no longer the one shown: say so and show the new version.
        const again = await shown.refetch()
        const moved = again.data?.item.digest !== digest
        setMessage(isChanged(e) || moved ? 'This changed while you were looking. Read it again, then turn it on.' : refusal(e))
        done()
      },
    ).finally(() => setBusy(false))
  }
  const turnOff = () => {
    setBusy(true)
    setMessage('')
    api.declineOrgItem(ws, kind, name).then(
      () => { done(); onClose() },
      (e) => setMessage(refusal(e)),
    ).finally(() => setBusy(false))
  }
  const setTool = (t: OrgTool, on: boolean) => {
    setBusy(true)
    setMessage('')
    api.setOrgTool(ws, name, t.name, on, on ? t.pin : undefined).then(
      () => void shown.refetch(),
      async (e) => {
        await shown.refetch()
        setMessage(isChanged(e) ? 'That tool changed while you were looking. Look again, then turn it on.' : refusal(e))
      },
    ).finally(() => setBusy(false))
  }

  const item = s?.item
  const hidden = item?.kind === 'skill' ? item.text_hidden : !!item?.fields_hidden
  const canOn = !!s && !!digest && !s.needs_isolation && !hidden && (s.status === 'off' || s.status === 'changed')
  const canOff = !!s && s.status !== 'off'
  return <Dialog open onOpenChange={(o) => !o && onClose()}>
    <DialogContent showCloseButton={false} className="data-[state=open]:animate-none data-[state=closed]:animate-none max-h-[85vh] overflow-y-auto sm:max-w-2xl" data-testid="company-sheet">
      <DialogTitle>{name}</DialogTitle>
      <DialogDescription className="sr-only">Read this company {kind === 'skill' ? 'skill' : 'tool server'} before turning it on.</DialogDescription>
      <div className="min-w-0 space-y-4 text-sm break-words">
        {shown.isPending && <p role="status">Loading…</p>}
        {shown.isError && <p role="alert">Could not load this. Get the latest from your hub and try again.</p>}
        {s && item && <>
          <p data-testid="company-sheet-status"><span className="font-medium">Now: </span>{STATUS_WORD[s.status]}</p>
          {s.status === 'changed' && <p className="text-warning">Your hub changed this since you last turned it on. Read it again.</p>}
          {item.kind === 'skill'
            ? hidden
              ? <p role="alert" data-testid="company-text-hidden">This text has characters that cannot be shown, so it cannot be turned on.</p>
              : <pre className="max-h-72 overflow-auto rounded-md border border-border bg-surface-2 p-3 text-[13px] whitespace-pre-wrap break-words" data-testid="company-skill-text">{plain(item.text)}</pre>
            : <>
              {hidden && <p role="alert" className="text-warning" data-testid="company-fields-hidden">Some details cannot be shown, so it cannot be turned on.</p>}
              {!!item.hidden_tools && <p className="text-warning" data-testid="company-hidden-tools">{item.hidden_tools} {item.hidden_tools === 1 ? 'tool' : 'tools'} could not be shown (they stay off).</p>}
              <ServerDetails item={item} tools={item.tools} serverOn={s.status === 'on'} busy={busy} onTool={setTool} />
            </>}
          <div className="space-y-1" data-testid="company-limits">
            <h3 className="font-medium">Good to know</h3>
            <ul className="list-disc space-y-1 pl-5">{limitLines(s).map((l) => <li key={l}>{l}</li>)}</ul>
          </div>
          {s.needs_isolation && <p role="alert" className="text-warning" data-testid="company-needs-isolation">Needs a separate agent user. On this computer it stays off.</p>}
        </>}
        {message && <p role="alert" className="text-warning" data-testid="company-message">{message}</p>}
      </div>
      <div className="flex flex-wrap justify-end gap-2">
        {canOn && <Action id="company-accept" disabled={busy} onClick={turnOn}>Turn on</Action>}
        {canOff && <Action id="company-decline" disabled={busy} onClick={turnOff}>Turn off</Action>}
        <Action id="company-close" onClick={onClose}>Close</Action>
      </div>
    </DialogContent>
  </Dialog>
}
