import { it } from 'vitest'
import { writeFileSync } from 'node:fs'
import ticket from '@/test/fixtures/server-m2/ticket.json'
import { FakeHost, READS, workspace } from '@/test/hostHarness'

const A = 'a'.repeat(32)
it('dump', async () => {
  const host = new FakeHost([workspace({ id: A, features: [...READS, 'publish', 'actions'], addOns: { available: true, add_ons: [{ id: 'publish', title: 'Publish', desc: 'Share files.', state: 'on', locked: null, blocked: null }] } })])
  const out: Record<string, unknown> = {}
  for (const p of ['/api/session', '/api/workspaces', '/api/me', `/api/w/${A}/identity`, `/api/w/${A}/today`, `/api/w/${A}/board`, `/api/w/${A}/tickets`, `/api/w/${A}/tickets/DEMO-0001`, `/api/w/${A}/tickets/DEMO-0001/events`, `/api/w/${A}/tickets/DEMO-0001/changes`, `/api/w/${A}/settings`, `/api/w/${A}/members`, `/api/w/${A}/add-ons`, `/api/w/${A}/grants`]) {
    out[p] = await (await host.fetch(p)).json()
  }
  writeFileSync('/private/tmp/claude-501/-Users-severin-Documents-GitHub-orch/6fcf73df-76d5-40ae-b503-8b1cdb1e1ede/scratchpad/dump.json', JSON.stringify({ A, out, ticket }))
})
