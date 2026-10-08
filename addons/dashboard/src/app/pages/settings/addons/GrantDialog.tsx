import type { AddonManifest } from '@/api/types'
import { SignPrompt } from '@/components/sign/SignPrompt'
import { CopyButton } from '../General'
import { addedCapabilities, explain, removedCapabilities } from './capabilities'

export const shortSha = (sha: string) => `${sha.slice(0, 8)}…${sha.slice(-6)}`

export type GrantAsk = { kind: 'grant'; addon: AddonManifest } | { kind: 'update'; addon: AddonManifest }

/**
 * Core's signing prompt for a capability grant or an update: name, version, package hash and what it may do.
 * The owner signs exactly these values (addon.granted {name, version, package_sha256, capabilities}).
 */
export function GrantDialog({ ask, onSign, onClose }: { ask: GrantAsk; onSign: () => void; onClose: () => void }) {
  const { addon } = ask
  const update = ask.kind === 'update' ? addon.update : null
  const version = update?.version ?? addon.version
  const sha = update?.package_sha256 ?? addon.package_sha256
  const caps = update?.capabilities ?? addon.capabilities
  const added = update ? addedCapabilities(addon.capabilities, caps) : []
  const removed = update ? removedCapabilities(addon.capabilities, caps) : []
  const title = update ? `Update ${addon.title} to ${version}` : `Grant ${addon.title} ${version}`

  return (
    <SignPrompt
      title={title}
      description={update ? 'An update needs a new grant. It stays off until you grant it again.' : undefined}
      covers={[
        `Addon: ${addon.name} ${version}`,
        caps.length ? `Capabilities: ${caps.join(', ')}` : 'Capabilities: none',
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
