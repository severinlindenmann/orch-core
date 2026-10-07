"""The vectors must catch a mutation of each rule that matters most in orch.remote.bridge_host.host_check: a mutant
that every host case still passes means the rule is not pinned by anything this suite runs."""
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

import test_bridge_host_vectors as V  # noqa: E402
from orch.remote.bridge_host import host_check  # noqa: E402

pytestmark = pytest.mark.slow  # re-runs the host cases against every mutant

SRC = Path(host_check.__file__).read_text(encoding="utf-8")

SEQ = ('        high, bitmap = self.store.seq_state(did)\n'
       '        ok, new_high, new_bitmap = seq_accept(high, bitmap, h.seq)\n'
       '        if not ok:\n'
       '            return self._recorded(h, did, dig, now, "stale_sequence", high=high)\n'
       '        self.store.save_seq(did, new_high, new_bitmap)\n')
TIME = ('        if abs(now - h.ts_ms) > WINDOW_MS:\n'
        '            return self._recorded(h, did, dig, now, "stale_timestamp", host_ms=now)\n')
MAL = ('        try:\n            meta, data = unframe(pt)\n        except Malformed:\n'
       '            return self._recorded(h, did, dig, now, "malformed")\n')

MUTANTS = {
    "time_before_sequence": (SEQ + "        # 7. time\n" + TIME, TIME + "        # 7. time\n" + SEQ),
    "malformed_after_sequence": (MAL + "        # 6. sequence BEFORE time: a stale_timestamp refusal consumes its seq, "
                                 "so the same bytes can never run\n" + SEQ, SEQ + MAL),
    "stale_timestamp_not_recorded": ('return self._recorded(h, did, dig, now, "stale_timestamp", host_ms=now)',
                                     'return refuse("stale_timestamp", host_ms=now)'),
    "stale_sequence_not_recorded": ('return self._recorded(h, did, dig, now, "stale_sequence", high=high)',
                                    'return refuse("stale_sequence", high=high)'),
    "accepted_not_recorded_before_it_runs": ("        self.store.record(rid, did, dig, now, None)\n", ""),
    "verified_request_dropped_by_the_budget": (
        "        # the signature verified: from here nothing is dropped for the budget\n",
        "        if not self.budget.take(now):\n            return drop('budget')\n"),
    "retention_follows_the_senders_clock": ('self.store.record(h.rid.hex(), did, dig, now, {"refusal"',
                                            'self.store.record(h.rid.hex(), did, dig, max(now, h.ts_ms), {"refusal"'),
    "known_rid_runs_again": ("        if known is not None:\n", "        if False:\n"),
    "replay_ignores_the_digest": ("if known.device != did or known.digest != dig:", "if known.device != did:"),
    "replay_host_ms_not_restamped": ('extra["host_ms"] = now', "pass"),
    "replay_high_not_restamped": ('extra["high"] = self.store.seq_state(did)[0]', "pass"),
    "window_strict": ("if abs(now - h.ts_ms) > WINDOW_MS:", "if abs(now - h.ts_ms) >= WINDOW_MS:"),
    "sequence_zero_allowed": ("if seq < 1 or i >= SEQ_WINDOW", "if i >= SEQ_WINDOW"),
    "bitmap_off_by_one": ("((bitmap << shift) | 1)", "((bitmap << (shift - 1)) | 1)"),
    "no_stream_ownership": ("if h.stream != ZERO_ID and self.streams.get(h.stream.hex()) != did:", "if False:"),
    "no_mailbox_id_check": ("if mailbox_id != h.rid.hex():", "if False:"),
    "revoked_not_refused": ("        if dev.revoked:\n            return self._unverified(now, \"revoked\")\n", ""),
}


def load(src: str, name: str):
    mod = types.ModuleType(name)
    mod.__file__ = host_check.__file__
    sys.modules[name] = mod  # dataclasses look their module up
    try:
        exec(compile(src, name, "exec"), mod.__dict__)
    finally:
        sys.modules.pop(name, None)
    return mod


def failures(tmp_path, mod) -> list[str]:
    real = V.Host
    V.Host = mod.Host
    out = []
    try:
        for k, case in enumerate(V.VEC["host_cases"]):
            try:
                if V.run_host_case(tmp_path / f"c{k}", case):
                    out.append(case["name"])
            except Exception:  # noqa: BLE001 - a mutant that crashes is caught too
                out.append(case["name"])
    finally:
        V.Host = real
    return out


def test_the_unmutated_module_passes_every_host_case(tmp_path):
    assert failures(tmp_path, load(SRC, "_bridge_host_base")) == []


@pytest.mark.parametrize("name", list(MUTANTS))
def test_mutant_is_caught(tmp_path, name):
    old, new = MUTANTS[name]
    assert SRC.count(old) == 1, f"mutant {name} no longer applies: update its anchor text"
    assert failures(tmp_path, load(SRC.replace(old, new), f"_bridge_host_{name}")), f"no vector catches {name}"
