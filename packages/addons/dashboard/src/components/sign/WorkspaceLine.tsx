// D6: which workspace a review signs in. The prefix and the end of the signed id come first; the name is the host's word
// (unsigned) and is never shown alone. C3: during the arm and in the risk state it is ALWAYS shown (`always`), with the
// target the View names (the ticket key); elsewhere only for a person with more than one workspace.
// Used by the answer strip (AnswerSend) and the generic click review (ClickSignPanel).
import type { ClickViewAnswer } from '@/api/click'
import { uuid8 } from '@/api/workspaces'
import { Raw } from './visible'

export function WorkspaceLine({ workspace, many, always = false, target, testId = 'sign-workspace' }: {
  workspace: ClickViewAnswer['view']['workspace'] | undefined
  /** The person has more than one workspace. */
  many: boolean
  /** The arm or the risk state: shown whatever the number of workspaces. */
  always?: boolean
  /** What the View acts on (its ticket key), from the View only. */
  target?: string | null
  testId?: string
}) {
  if ((!many && !always) || !workspace?.id || !workspace.prefix) return null
  return (
    <p data-testid={testId} className="text-[12px] text-text-muted">
      Workspace <Raw>{workspace.prefix}</Raw> · <span className="font-mono" data-ws-id>{uuid8(workspace.id)}</span>
      {workspace.name && <> · <bdi><Raw>{workspace.name}</Raw></bdi></>}
      {target && <> · <span data-testid={`${testId}-target`}><Raw>{target}</Raw></span></>}
    </p>
  )
}
