import { describe, expect, it } from 'vitest'
import { queries } from './queries'

describe('live-invalidated reads are not re-read on every page change', () => {
  it('keeps identity and the workspace list fresh longer than the 5 s default', () => {
    expect(queries.me('w1').staleTime).toBeGreaterThanOrEqual(30_000)
    expect(queries.workspaces().staleTime).toBeGreaterThanOrEqual(30_000)
  })
})
