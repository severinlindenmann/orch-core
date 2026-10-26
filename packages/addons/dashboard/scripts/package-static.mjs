import { cp, mkdir, rm } from 'node:fs/promises'
const target = new URL('../server/src/orch_dashboard/static/', import.meta.url)
await rm(target, { recursive: true, force: true })
await mkdir(target, { recursive: true })
await cp(new URL('../dist/', import.meta.url), target, { recursive: true })
