import { describe, expect, it } from 'vitest'
import { approversText, changesNote, commitCover, policySentence } from './gates'
import type { TicketBranch } from './types'

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

describe('W23: the commit line never ends in an empty "against"', () => {
  const b = { name: 'feat/x', head: '5be3d10', base: '', files: 0, additions: 0, deletions: 0, commits: [] } as unknown as TicketBranch
  it('no base and no counts: only the commit and branch', () => {
    expect(commitCover(b)).toBe('Commit 5be3d10 on feat/x')
    expect(changesNote(b)).toBe('')
  })
  it('a base: the full line as before', () => {
    const full = { ...b, base: 'develop', files: 3, additions: 40, deletions: 12 }
    expect(commitCover(full)).toBe('Commit 5be3d10 on feat/x: +40 −12 in 3 files against develop')
    expect(changesNote(full)).toBe('+40 −12 against develop')
  })
})
