// The server sends `script-src 'self'` (no eval): zod's compiled fast path probes `new Function("")`, and a strict CSP
// reports even that caught probe as a violation on every page load. Import this FIRST in every entry file, before any
// module builds a schema (zod reads the flag when it builds an object schema).
import { z } from 'zod'

z.config({ jitless: true })
