# Where the phone's vectors come from

The vectors are not copied into this package. `Vectors.swift` and `Fixtures.swift` read them by path from the
repository root, so a change to a vector is one commit that every consumer sees at once (monorepo, `contracts/`).

| Vectors | Read from | Owner |
|---|---|---|
| relay protocol v2 (`vectors_v2.json`) and HTTP examples (`http_fixtures/`, incl. push registration) | `contracts/relay/` | relay |
| hub contract (`vectors_hub.json`), plain-http origin rule (`origin_http.json`) | `contracts/hub/`, `contracts/origin_http.json` | hub |
| F1 phone/question/hash vectors | `contracts/core/f1/` | core |
| reader known-answer test (`reader_kat_1.json`) | `packages/core/src/orch/identity/` (ships in core's wheel) | core |
| R2 framing vectors (`r2_framing.json`; also the source of `PhoneRetryTableGenerated.swift`) | `contracts/core/r2_framing.json` | core |
| Unicode 16 tables (source of `Unicode16Data.swift`) | `packages/core/src/orch/canon/_unicode16.py` | core |
| `reader_decl_vectors.json` | this folder (a trimmed, derived extract of the relay vectors' `reader_decl` group, H12d) | phone |

Regenerate the generated Swift files from the repository root:
`python3 packages/mobile/OrchKit/Scripts/gen_retry_table.py` and
`python3 packages/mobile/OrchKit/Scripts/gen_unicode16_swift.py packages/core/src/orch/canon/_unicode16.py <core sha>`
(`--check` fails when the file drifted).

Notes carried over from the old copies:

- `text.json` (`lone_surrogate`) and `hashes.json` (`value_hash_refused`) contain a lone UTF-16 surrogate escape that a Swift string cannot
  hold. The loader replaces the escape with U+FFFD before parsing; the `lone_surrogate` text case is skipped by name.
- Not implemented here (SkipList.swift): the relay's `p3.restore` vectors and the bridge_host_post cases
  `expired_identical_chunk_restored` and `refusal_409_retry_exhausted`.
