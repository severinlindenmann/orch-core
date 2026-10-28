// D6: which workspace a review signs in, for a person with more than one. The prefix and the end of the signed id come
// first; the name is the host's word (unsigned) and is never shown alone. One workspace: nothing (there is no choice to
// mix up). Used by the answer strip (AnswerSend) and the generic click review (ClickSignPanel).
import type { ClickViewAnswer } from '@/api/click'
import { uuid8 } from '@/api/workspaces'
import { Raw } from './visible'

export function WorkspaceLine({ workspace, many, testId = 'sign-workspace' }: { workspace: ClickViewAnswer['view']['workspace'] | undefined; /** The person has more than one workspace. */ many: boolean; testId?: string }) {
  if (!many || !workspace?.id || !workspace.prefix) return null
  return (
    <p data-testid={testId} className="text-[12px] text-text-muted">
      Workspace <Raw>{workspace.prefix}</Raw> · <span className="font-mono" data-ws-id>{uuid8(workspace.id)}</span>
      {workspace.name && <> · <bdi><Raw>{workspace.name}</Raw></bdi></>}
    </p>
  )
}
