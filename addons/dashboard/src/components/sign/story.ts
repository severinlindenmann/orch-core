// What a signature leads to, in plain words, worked out from the ACTION that is being signed (never typed by hand per
// screen): the button that says what happens, the "What happens next" steps, and the agent access terms. A step is
// listed only when the action really does it. An action this file does not know gets the plain "Sign" and no steps.
import { accessLength } from '@/api/grants'
import type { ClickAction } from '@/api/click'
import type { QuickTyped } from './alsoSigned'

export interface SignStory {
  /** The button: what happens when it is pressed. */
  signLabel: string
  /** "What happens next", each step true for this action. Empty: nothing is promised. */
  steps: string[]
  /** One plain sentence under the content for a signature with a narrower scope than it may sound. */
  scope?: string
  /** Agent access given by this signature: its length in words. */
  access?: { length: string }
  /** The signed content is shown short (what is answered), the rest under Details; the dialog closes by itself when signed. */
  compact?: boolean
  /** A quick task: what was typed, so the review can fold the derived rows under "Also signed". */
  quick?: QuickTyped
}

/** What agents may do with the access, in the words of the review (the same for every grant the dashboard signs). */
export const AGENT_MAY = 'Agents (Claude Code) may read and change files in the project folder and run its commands.'

export interface StoryContext {
  /** An agent is started by the page after this signature (the caller's own start plan), with access given now or already there. */
  startsAgent?: boolean
  /** The signer's own person id (a quick task's owner row must be this person to fold). */
  person?: string | null
}

export function storyOf(action: ClickAction | null | undefined, ctx: StoryContext = {}): SignStory | null {
  if (!action) return null
  switch (action.action) {
    case 'new':
    case 'new_sequence': {
      const hours = action.action === 'new_sequence' ? action.agent?.hours : undefined
      if (hours) return agentCreate(hours)
      if (ctx.startsAgent) return { signLabel: 'Sign and create ticket', steps: ['You sign.', 'The ticket is created.', 'An agent starts, with the access you already gave, and writes the requirements and a plan.', 'It stops and waits: nothing is built until you approve the plan.'] }
      return { signLabel: 'Sign and create ticket', steps: ['You sign.', 'The ticket is created.', 'Nothing starts yet: an agent starts only when you press Start agent.'] }
    }
    case 'quick_start': {
      const hours = action.agent?.hours
      const access = hours ? { length: accessLength(hours) } : undefined
      return {
        signLabel: ctx.startsAgent ? 'Sign and start task' : 'Sign and create task',
        access,
        quick: { title: action.title, details: action.description ?? '', checks: action.checks ?? [], signer: ctx.person },
        steps: ['You sign.', 'The task is created, with the requirements and the plan you see here approved.', ...(hours ? [`Access is given for ${accessLength(hours)}.`] : []), ...(ctx.startsAgent ? ['An agent starts and does the work.'] : ['Nothing starts yet: an agent starts only when you press Start agent.'])],
      }
    }
    case 'approve':
      if (action.gate === 'requirements') return { signLabel: 'Sign requirements', scope: 'This signs the requirements only, not the plan.', steps: ['You sign.', 'The requirements are locked as shown.', 'The plan still needs your approval, separately.'] }
      if (action.gate === 'plan') return { signLabel: 'Sign and start building', steps: ['You sign.', 'The agent may start building the plan shown here.', 'You review the result and give the final verdict.'] }
      return { signLabel: 'Sign approval', steps: ['You sign.', 'Your approval of this step is recorded as shown.'] }
    case 'market_grant':
      return { signLabel: 'Approve and install', steps: ['You sign approval for this exact package and its permissions.', 'The host installs the approved package.'] }
    case 'answer':
      return { signLabel: 'Send answer', steps: [], compact: true }
    case 'runner_grant': {
      const length = accessLength(action.hours)
      return action.extend
        ? { signLabel: 'Sign and extend', access: { length }, steps: ['You sign.', `The old access ends and the new one starts, for ${length} from now.`, 'Running agents stop. Start them again to use the new access.'] }
        : ctx.startsAgent
          ? { signLabel: 'Sign and start agent', access: { length }, steps: ['You sign.', `Agents may work for you for ${length} from now.`, 'The agent starts.'] }
          : { signLabel: 'Sign and allow access', access: { length }, steps: ['You sign.', `Agents may work for you for ${length} from now.`] }
    }
    default:
      return null
  }
}

function agentCreate(hours: number): SignStory {
  const length = accessLength(hours)
  return {
    signLabel: 'Sign and start agent',
    access: { length },
    steps: ['You sign.', `The ticket is created and access is given for ${length}.`, 'An agent starts and writes the requirements and a plan for you.', 'It stops and waits: nothing is built until you approve the plan.'],
  }
}
