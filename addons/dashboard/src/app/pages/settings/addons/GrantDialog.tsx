import { manifestFor, manifestProblem, pendingUpdate, viewerActions } from '@/api/addons'
import type { AddonPackage, InstalledAddon } from '@/api/types'
import { addonName, Raw, wordsAndId } from '@/addon-ui/SignConfirm'
import { plain } from '@/components/sign/visible'
import { SignPrompt } from '@/components/sign/SignPrompt'
import { addedCapabilities, explain, removedCapabilities } from './capabilities'

export const shortSha = (sha: string) => `${sha.slice(0, 8)}…${sha.slice(-6)}`

export type GrantAsk = { kind: 'grant'; addon: InstalledAddon } | { kind: 'update'; addon: InstalledAddon } | { kind: 'install'; addon: AddonPackage }

/**
 * Core's signing prompt for a capability grant or an update: name, version, package hash and what it may do.
 * The owner signs exactly these values (addon.granted {name, version, package_sha256, capabilities}).
 * Installing and granting are one signed act that also turns the addon on ("Grant and turn on").
 */
export function GrantDialog({ ask, onSign, onClose }: { ask: GrantAsk; onSign: () => void; onClose: () => void }) {
  const { addon } = ask
  // A catalog install has no workspace state yet: the package itself is what is offered.
  const installed = ask.kind === 'install' ? { version: ask.addon.version, package_sha256: ask.addon.package_sha256, capabilities: ask.addon.capabilities } : ask.addon.ws
  const update = ask.kind === 'update' ? pendingUpdate(ask.addon) : null
  const version = update?.version ?? installed.version
  const sha = update?.package_sha256 ?? installed.package_sha256
  const caps = update?.capabilities ?? installed.capabilities
  const added = update ? addedCapabilities(installed.capabilities, caps) : []
  const removed = update ? removedCapabilities(installed.capabilities, caps) : []
  const viewerNow = viewerActions(manifestFor(addon as AddonPackage, installed.version))
  const viewerNext = update ? viewerActions({ actions: update.actions ?? addon.actions }) : viewerNow
  const turnsOn = ask.kind !== 'update'
  const viewerAdded = viewerNext.filter((a) => !viewerNow.some((b) => b.id === a.id))
  const viewerRemoved = viewerNow.filter((a) => !viewerNext.some((b) => b.id === a.id))
  // Core's words for the action ids that are signed (viewer_actions); the manifest's labels are the addon's, shown apart.
  const viewersCan = `Viewers can: ${viewerNext.length ? viewerNext.map((a) => wordsAndId(a.id)).join(', ') : 'nothing'}`
  const labelled = viewerNext.filter((a) => a.label !== a.id)
  // The addon by its manifest title (it could say anything) and always its package id.
  const named = addonName(addon.title, addon.name)
  // A name or title core cannot say plainly in its own lines is never signed (the host refuses it too).
  const bad = manifestProblem(addon)
  // An update keeps the addon's on/off state (the host does not touch it): say which one it is.
  const staysOn = ask.kind === 'update' && ask.addon.ws.enabled
  // Version and capabilities come from the package: every invisible character shown (plain), as for any addon string.
  const v = plain(version)
  const title = update ? `Update ${named} to ${v}` : `${ask.kind === 'install' ? 'Install' : 'Grant'} ${named} ${v}`

  return (
    <SignPrompt
      title={title}
      description={update ? 'Signing grants the new version. The addon stays as it is now (on or off).' : 'One signature grants these capabilities and turns the addon on.'}
      covers={[
        `Addon: ${plain(addon.name)} ${v}`,
        caps.length ? `Capabilities: ${caps.map(plain).join(', ')}` : 'Capabilities: none',
        viewersCan,
        ...(turnsOn ? ['Grants these capabilities and turns it on in this workspace'] : []),
        ...(update ? [staysOn ? `The new capabilities take effect now and ${named} stays on` : `${named} stays off; the new capabilities apply when it is turned on`] : []),
        ...(caps.includes('pty') ? ['Agents never get pty'] : []),
      ]}
      confirmLabel={update ? 'Update' : 'Grant and turn on'}
      disabled={!!bad}
      hash={`sha256:${sha}`}
      onSign={onSign}
      onClose={onClose}
    >
      <div className="space-y-3 text-[13px]">
        {bad && (
          <p role="alert" className="text-danger">
            Core cannot sign this package. {bad}
          </p>
        )}
        <div className="flex items-center gap-2">
          <span className="font-medium">{named}</span>
          <Raw>{version}</Raw>
        </div>
        {update && (
          <section aria-label="From the addon: changelog" className="rounded-md border border-dashed border-border p-2 text-text-muted">
            <p className="mb-0.5 text-[12px]">From the addon: changelog</p>
            <p className="break-words text-text">{update.changelog}</p>
          </section>
        )}
        {update && (added.length > 0 || removed.length > 0) && (
          <ul aria-label="Capability changes" className="space-y-0.5 font-mono">
            {added.map((c) => (
              <li key={c} className="text-success">{`+ ${plain(c)}`}</li>
            ))}
            {removed.map((c) => (
              <li key={c} className="text-danger">{`- ${plain(c)}`}</li>
            ))}
          </ul>
        )}
        {(viewerAdded.length > 0 || viewerRemoved.length > 0) && (
          <ul aria-label="Viewer action changes" className="space-y-0.5 font-mono">
            {viewerAdded.map((a) => (
              <li key={a.id} className="text-success">{`+ Viewers can: ${wordsAndId(a.id)}`}</li>
            ))}
            {viewerRemoved.map((a) => (
              <li key={a.id} className="text-danger">{`- Viewers can: ${wordsAndId(a.id)}`}</li>
            ))}
          </ul>
        )}
        {labelled.length > 0 && (
          <section aria-label="From the addon: viewer action labels" className="rounded-md border border-dashed border-border p-2 text-text-muted">
            <p className="mb-0.5 text-[12px]">From the addon: what it calls these actions</p>
            {labelled.map((a) => (
              <p key={a.id} className="break-words">
                <Raw>{a.id}</Raw>: <span className="text-text">{a.label}</span>
              </p>
            ))}
          </section>
        )}
        <ul className="space-y-1">
          {caps.map((c) => (
            <li key={c} className="flex gap-2">
              <Raw>{c}</Raw>
              <span className="text-text-muted">{explain(c)}</span>
            </li>
          ))}
          {!caps.length && <li className="text-text-muted">Asks for no capabilities.</li>}
        </ul>
      </div>
    </SignPrompt>
  )
}
