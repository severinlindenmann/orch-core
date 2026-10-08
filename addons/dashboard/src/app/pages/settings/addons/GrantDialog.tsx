import { pendingUpdate, viewerActions } from '@/api/addons'
import type { InstalledAddon } from '@/api/types'
import { SignPrompt } from '@/components/sign/SignPrompt'
import { CopyButton } from '../General'
import { addedCapabilities, explain, removedCapabilities } from './capabilities'

export const shortSha = (sha: string) => `${sha.slice(0, 8)}…${sha.slice(-6)}`

export type GrantAsk = { kind: 'grant'; addon: InstalledAddon } | { kind: 'update'; addon: InstalledAddon }

/**
 * Core's signing prompt for a capability grant or an update: name, version, package hash and what it may do.
 * The owner signs exactly these values (addon.granted {name, version, package_sha256, capabilities}).
 */
export function GrantDialog({ ask, onSign, onClose }: { ask: GrantAsk; onSign: () => void; onClose: () => void }) {
  const { addon } = ask
  const installed = addon.ws
  const update = ask.kind === 'update' ? pendingUpdate(addon) : null
  const version = update?.version ?? installed.version
  const sha = update?.package_sha256 ?? installed.package_sha256
  const caps = update?.capabilities ?? installed.capabilities
  const added = update ? addedCapabilities(installed.capabilities, caps) : []
  const removed = update ? removedCapabilities(installed.capabilities, caps) : []
  const viewerNow = viewerActions(addon)
  const viewerNext = update ? viewerActions({ actions: update.actions ?? addon.actions }) : viewerNow
  const viewerAdded = viewerNext.filter((a) => !viewerNow.some((b) => b.id === a.id))
  const viewerRemoved = viewerNow.filter((a) => !viewerNext.some((b) => b.id === a.id))
  const viewersCan = `Viewers can: ${viewerNext.length ? viewerNext.map((a) => a.label).join(', ') : 'nothing'}`
  const title = update ? `Update ${addon.title} to ${version}` : `Grant ${addon.title} ${version}`

  return (
    <SignPrompt
      title={title}
      description={update ? 'An update needs a new grant. It stays off until you grant it again.' : undefined}
      covers={[
        `Addon: ${addon.name} ${version}`,
        caps.length ? `Capabilities: ${caps.join(', ')}` : 'Capabilities: none',
        viewersCan,
        `Package: sha256 ${shortSha(sha)}`,
        'Agents never enable addons and never get pty',
      ]}
      confirmLabel={update ? 'Update' : 'Grant and sign'}
      onSign={onSign}
      onClose={onClose}
    >
      <div className="space-y-3 text-[13px]">
        <div className="flex items-center gap-2">
          <span className="font-medium">{addon.title}</span>
          <span className="font-mono text-text-muted">{version}</span>
        </div>
        <div className="flex items-center gap-1 text-text-muted">
          <span>sha256</span>
          <code className="font-mono text-text" title={sha}>
            {shortSha(sha)}
          </code>
          <CopyButton value={sha} label="package hash" />
        </div>
        {update && <p className="text-text-muted">{update.changelog}</p>}
        {update && (added.length > 0 || removed.length > 0) && (
          <ul aria-label="Capability changes" className="space-y-0.5 font-mono">
            {added.map((c) => (
              <li key={c} className="text-success">{`+ ${c}`}</li>
            ))}
            {removed.map((c) => (
              <li key={c} className="text-danger">{`- ${c}`}</li>
            ))}
          </ul>
        )}
        <p className="text-text-muted">{viewersCan}</p>
        {(viewerAdded.length > 0 || viewerRemoved.length > 0) && (
          <ul aria-label="Viewer action changes" className="space-y-0.5 font-mono">
            {viewerAdded.map((a) => (
              <li key={a.id} className="text-success">{`+ Viewers can: ${a.label}`}</li>
            ))}
            {viewerRemoved.map((a) => (
              <li key={a.id} className="text-danger">{`- Viewers can: ${a.label}`}</li>
            ))}
          </ul>
        )}
        <ul className="space-y-1">
          {caps.map((c) => (
            <li key={c} className="flex gap-2">
              <code className="font-mono">{c}</code>
              <span className="text-text-muted">{explain(c)}</span>
            </li>
          ))}
          {!caps.length && <li className="text-text-muted">Asks for no capabilities.</li>}
        </ul>
      </div>
    </SignPrompt>
  )
}
