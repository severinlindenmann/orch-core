import { describe, expect, it } from 'vitest'
import { approversText, policySentence } from './gates'

describe('gate policy wording', () => {
  it('names the approver groups in words', () => {
    expect(approversText('owner')).toBe('owners')
    expect(approversText('maintainer')).toBe('owners or maintainers')
    expect(approversText('reviewers')).toBe("the ticket's reviewers")
  })
  it('says the policy in one sentence', () => {
    expect(policySentence('Plan', { approvers: 'maintainer', count: 1 })).toBe('Plan needs 1 approval from owners or maintainers.')
    expect(policySentence('Verify', { approvers: 'reviewers', count: 2, not: 'assignees' })).toBe("Verify needs 2 approvals from the ticket's reviewers, not the assignees.")
  })
})
