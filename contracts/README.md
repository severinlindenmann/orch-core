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
| `hub/vectors_published.json` | M4 Published (links and apps, #1357 S0): slugs, reach origins and CSP, password record, version meta and commit binding, owner approval on the hub AND direct path, company-list rows, `hub.release_narrow`, `hub.app_pass` (sections 11.4 to 11.9) | `hub/make_vectors_published.py` | hub `test_published_vectors` (reference, builds the file), publish `test_published_vectors`, core `tests/hub/test_published_vectors` |
| `hub/protocol.md` | the hub contract (H0), written against `relay/protocol-v2.md` | hub | all of the above |
| `relay/vectors_v2.json` | relay protocol v2 vectors (suite 2) | relay | core `tests/wsproto`, `tests/crypto`, `tests/canon`; relay `test_proto`, `test_push_vectors`; publish (derived `publish_v2.json`); mobile (`VectorTests`) |
| `relay/http_fixtures/` | relay HTTP v2 examples, pinned by their own `MANIFEST.json` | relay | core `tests/wsproto/test_corpus.py`, mobile (`OrchRelayFixtureTests`, push fixtures) |
| `relay/protocol-v2.md` | the relay protocol | relay | all |
| `publish/publish_v2.json`, `publish_v2_extra.json` | publish-request and card vectors (suite-2 subset of the relay vectors, plus edge cases) | publish (`scripts/extract_publish_vectors.py`, `make_extra_vectors.py`) | core `tests/publish`, hub `test_core_crosscheck`, publish tests |
| `core/f1/` | F1 vectors: ticket log, hashes, questions, labels, phone events, checkpoints, ... | core (`tests/canon/oracle_f1.py`, an oracle independent of core's code) | core (many test areas), hub (`test_core_crosscheck`, `test_question_spec`, `test_webapi_core`, JS), mobile (`F1*`, `PhoneEvents*`, `GateReview*`), dashboard server (`test_text`) |
| `core/followup_addon_event_vectors.json` | add-on events in follow-ups (#718, addon-api 1.2): `addon.granted` with `events`, `followup.set` on an add-on event (with and without a status filter), `followup.fired` with the add-on event, and the refusals (undeclared status, another add-on's kind, ref of the wrong format, event outside the set..wake window). Unsigned, model-level scenarios | core | core `tests/model/test_followup_addon_vectors.py` (the hub summary, dashboard and phone render these shapes: replay the same steps if they check them) |
| `core/r2_framing.json` | R2 bridge framing vectors; also the source of the phone's `PhoneRetryTableGenerated.swift` | none: FROZEN. Its producer, the host bridge `orch.relay`, was deleted in R4c (`b31b5befe`); the generator went with it (#1300). To regenerate, check out `b31b5befe^` | mobile (`R2FramingVectorTests`, `gen_retry_table.py --check`) |

`formats/<kind>.json` (`host`, `workspace`, `hub`, `relay`, `publish`) are not protocol vectors: they are the baseline of
the persisted-format guard (`scripts/formats.py`, checked by each package's `tests/test_format_guard.py`; see
`docs/architecture/persisted-formats.md`). Regenerate one only with `python scripts/formats.py --update <kind>`, after the
migration step and the `CURRENT` bump.

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
  and `uv run python ../../contracts/hub/make_vectors_hub_policy.py`, and `uv run python ../../contracts/hub/make_vectors_published.py`.
- Publish: `python packages/publish/scripts/extract_publish_vectors.py` (from `contracts/relay/vectors_v2.json`).
- Phone tables: `python3 packages/mobile/OrchKit/Scripts/gen_retry_table.py` (and `--check`).
