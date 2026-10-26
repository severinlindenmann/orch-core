# contracts

Protocol documents and test vectors that more than one package depends on. Each file exists once, here. A package
that needs one reads it from this folder (tests use a path from the repository root, the phone reads it by
`#filePath`); nothing is copied into a package, so there is no sync script and no pin file.

Changing a file here changes a signed or wire shape for every consumer below. Treat it as a contract change: update
every consumer in the same commit, list them in the PR, and expect the `contracts` and `mobile` workflows to run.

| File | What | Producer | Consumers |
|---|---|---|---|
| `origin_http.json` | the one plain-http origin rule (localhost, private, CGNAT) | hub | core `tests/wsproto`, hub, relay, publish, mobile (`OriginHTTPTests`) |
| `hub/vectors_hub.json` | hub contract H0 vectors (card, cert, link, channel, custodial, space, ...), FAKE keys | hub (`orch_hub.contract.vectorgen`) | core `tests/hub`, relay `test_hub_*`, publish `test_hub_*`, mobile (`HubVectorTests`, custody and push tests), hub's JS |
| `hub/vectors_hub_policy.json` | `orch.hub-policy/1` vectors | `hub/make_vectors_hub_policy.py` | core `tests/hub`, `tests/market`, `tests/addons`, hub `test_hub_policy*` |
| `hub/protocol.md` | the hub contract (H0), written against `relay/protocol-v2.md` | hub | all of the above |
| `relay/vectors_v2.json` | relay protocol v2 vectors (suite 2) | relay | core `tests/wsproto`, `tests/crypto`, `tests/canon`; relay `test_proto`, `test_push_vectors`; publish (derived `publish_v2.json`); mobile (`VectorTests`) |
| `relay/http_fixtures/` | relay HTTP v2 examples, pinned by their own `MANIFEST.json` | relay | core `tests/wsproto/test_corpus.py`, mobile (`OrchRelayFixtureTests`, push fixtures) |
| `relay/protocol-v2.md` | the relay protocol | relay | all |
| `publish/publish_v2.json`, `publish_v2_extra.json` | publish-request and card vectors (suite-2 subset of the relay vectors, plus edge cases) | publish (`scripts/extract_publish_vectors.py`, `make_extra_vectors.py`) | core `tests/publish`, hub `test_core_crosscheck`, publish tests |
| `core/f1/` | F1 vectors: ticket log, hashes, questions, labels, phone events, checkpoints, ... | core (`tests/canon/oracle_f1.py`, an oracle independent of core's code) | core (many test areas), hub (`test_core_crosscheck`, `test_question_spec`, `test_webapi_core`, JS), mobile (`F1*`, `PhoneEvents*`, `GateReview*`), dashboard server (`test_text`) |
| `core/r2_framing.json` | R2 bridge framing vectors; also the source of the phone's `PhoneRetryTableGenerated.swift` | core (`scripts/gen_r2_framing_vectors.py`) | mobile (`R2FramingVectorTests`, `gen_retry_table.py --check`) |

Two files stay next to their code on purpose:

- `packages/core/src/orch/identity/reader_kat_1.json` ships inside core's wheel (the host reads it at run time); the F1
  oracle writes it there, and the phone's `ReaderDeclTests` read it from there.
- `packages/publish/pubsrv/relay_proto.py` is a byte copy of `packages/relay/src/orch_relay/proto/__init__.py`, because
  publish installs on its own and cannot import the relay package. A publish test fails when the two differ.

The phone also keeps `OrchKit/Tests/OrchTestVectors/reader_decl_vectors.json`, a trimmed extract of the relay vectors'
`reader_decl` group (H12d), and generates its Unicode 16 tables from core's `src/orch/canon/_unicode16.py`.

## Regenerating

- F1: `cd packages/core && uv run python -m tests.canon.oracle_f1` (a test fails when a committed file differs from the oracle).
- Hub vectors: `cd packages/hub && uv run python -m orch_hub.contract.vectorgen > ../../contracts/hub/vectors_hub.json`,
  and `uv run python ../../contracts/hub/make_vectors_hub_policy.py`.
- Publish: `python packages/publish/scripts/extract_publish_vectors.py` (from `contracts/relay/vectors_v2.json`).
- Phone tables: `python3 packages/mobile/OrchKit/Scripts/gen_retry_table.py` (and `--check`).
