import type { Workspace } from '@/api/types'
import type { AddOnRow } from '@/api/addOnSwitches'
import type { SignContent } from '@/api/signing'
import { SignPopover } from '@/components/sign/SignPopover'
/** Presentation only: the host's lines, notes and risk are shown as sent (the trust-line rule lives in the host's signcontent.py). */
export function capabilityWords(value: string): string {
  const names: Record<string, string> = { files: 'files', filesystem: 'files', read_files: 'read files', write_files: 'change files', pty: 'terminals', network: 'internet', spawn_agent: 'agents', serve_http: 'web pages', git_push: 'push code', choose_model: 'choose agent models', credential: 'saved sign-in keys', secrets: 'service tokens' }
  return names[value] ?? value
}

export function addonContent(content: SignContent, _marketplace?: boolean): SignContent {
  const lines = content.summary?.lines.filter(l => !l.startsWith('Version ')).map(l => l.startsWith('It says it will ') ? l.replace(/\b[a-z][a-z_]+\b/g, capabilityWords) : l)
  return content.summary && lines ? { ...content, summary: { ...content.summary, lines } } : content
}

export function AddonReview({ workspace, row, onBusy, onDone, onClose }: {
  workspace: Workspace; row: AddOnRow; marketplace: boolean; onBusy: (busy: boolean) => void; onDone: () => void; onClose: () => void
}) {
  const turningOn = row.state !== 'on'
  return <SignPopover workspace={workspace} ws={workspace.id} title={`${turningOn ? 'Turn on' : 'Turn off'} ${row.title}`} action={{ action: turningOn ? 'addon_grant' : 'addon_disable', name: row.id }} inline onSigning={onBusy} onClose={onClose} onSettled={phase => { if (phase === 'done') onDone() }} />
}
