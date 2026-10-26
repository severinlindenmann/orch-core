"""Runs an F1 scenario vector (``tests/vectors/f1/*.json`` ``scenarios``) through ``orch.model`` with the real
``CryptoVerifier``: every step is offered to ``admit`` on the accepted prefix. ``replay_only`` steps must refuse
append with ``chain.bad_ws_seq`` but replay successfully. Other refused events are also replayed as the next line
of the log; optional ``after`` checks read the state after an accepted step. ``schema_refused`` steps also check
``append_code`` at the store append validation boundary, independently of the model outcome in ``expect``."""

from __future__ import annotations

import copy
import json
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from orch import crypto, schema
from orch.identity import CryptoVerifier
from orch.model import WORKSPACE, admit, replay
from orch.model.codes import Code
from orch.model.engine import CHAIN_CODES
from orch.store import Store, StoreError

DIR = Path(__file__).resolve().parents[4] / "contracts" / "core" / "f1"

# §5.5 "Reading": a line whose person ``sig`` does not verify under the certificate of ``actor.device`` breaks the chain
# at that line (on replay it is a chain error, not an authorization failure).
F1_CHAIN_BREAKS = {"sig.invalid"}


def load(name: str) -> Any:
    return json.loads((DIR / name).read_text(encoding="utf-8"))


def thaw(o: Any) -> Any:
    if isinstance(o, dict) or hasattr(o, "items"):
        return {k: thaw(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [thaw(v) for v in o]
    return o


def state_of(sc: dict[str, Any], ws: list, tl: dict[str, list]):
    return replay(
        ws,
        tl,
        verifier=CryptoVerifier(),
        now=sc["now"],
        expected_workspace_id=sc["workspace_id"],
        expected_genesis=sc.get("genesis") if ws else None,  # pinned only once the genesis is in
    )


def check_after(st: Any, after: dict[str, Any]) -> None:
    if "workspace" in after:
        w, exp = st.workspace, after["workspace"]
        if "devices_revoked" in exp:
            assert sorted(d.id for d in w.devices.values() if d.revoked) == sorted(exp["devices_revoked"])
        if "roster_v" in exp:
            assert w.roster_v == exp["roster_v"]
        if "members" in exp:
            assert {p: m.role for p, m in w.members.items()} == exp["members"]
    for uid, exp in after.get("tickets", {}).items():
        t = st.tickets[uid]
        if "gens" in exp:
            assert [t.gates[g].gen for g in ("requirements", "plan", "verify", "code")] == exp["gens"], uid
        if "status" in exp:
            assert t.status == exp["status"], uid
        if "approved" in exp:
            assert sorted(g for g, v in t.gates.items() if v.approved) == sorted(exp["approved"]), uid
        for g, h in exp.get("hash", {}).items():
            assert t.gates[g].hash == h, (uid, g)
        for g, gin in exp.get("G", {}).items():
            assert thaw(t.gates[g].input) == gin, (uid, g)
        if "blocked" in exp:
            assert sorted(g for g, v in t.gates.items() if v.blocked) == sorted(exp["blocked"]), uid
        if "frozen" in exp:
            assert t.frozen == exp["frozen"], uid
        if "source_list" in exp:
            assert [dict(x) for x in t.source_list] == exp["source_list"], uid
        if "counting" in exp:
            for g in t.gates:
                assert sorted(t.gates[g].counting) == sorted(exp["counting"].get(g, [])), (uid, g)
        if "gens" in exp:  # 5.7: a settled ticket lists a revoked device's decisions that still count, per gate
            for g in t.gates:
                assert sorted(t.gates[g].revoked_decisions) == exp.get("revoked_decisions", {}).get(g, []), (uid, g)
        if "source_list" not in exp and "gens" in exp:
            assert not t.source_list, uid
        if "policy" in exp:
            assert {g: thaw(t.gates[g].policy) for g in t.gates} == exp["policy"], uid
        for q, want in exp.get("answers", {}).items():  # H12e: the answer event that counts, per question
            core = st._core.tickets[uid].questions[q]
            assert (core.answer["event"] if core.answer else None) == want, (uid, q)
        if "policy_hash" in exp:
            assert {g: t.gates[g].policy_hash for g in exp["policy_hash"]} == exp["policy_hash"], uid


def _gens(st: Any, uid: str) -> list[int]:
    t = st.tickets.get(uid)
    return [t.gates[g].gen for g in ("requirements", "plan", "verify", "code")] if t else [0, 0, 0, 0]


class VectorSigner:
    def __init__(self) -> None:
        key = load("signatures.json")["keys"]["wsk"]["scalar_hex"]
        self.key = crypto.private_key_from_scalar(int(key, 16))
        self.public_key = crypto.public_bytes(self.key)

    def sign(self, payload: bytes) -> bytes:
        return crypto.sign(self.key, payload)


def check_append_code(sc: dict[str, Any], st: Any, step: dict[str, Any], where: str) -> None:
    # Exercise the store's append validation boundary with the vector's exact stamped envelope.
    # Projection healing and wall-clock stamping are not part of these log-only scenarios.
    event = copy.deepcopy(step["event"])
    with TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as patch:
        root = Path(tmp)
        (root / "workspace").mkdir()
        with closing(
            Store.open(
                root / "workspace",
                expected_workspace_id=sc["workspace_id"],
                host=VectorSigner(),
                host_state_dir=root / "host",
                create=True,
            )
        ) as store:
            store._state = st
            patch.setattr(store, "_stamp", lambda *args, **kwargs: None)
            with pytest.raises(StoreError) as exc:
                store._commit(event, step["log"], None, {}, ())
            assert exc.value.code == step["append_code"], where


def run_scenario(sc: dict[str, Any], *, validate: bool = True) -> tuple[list, dict[str, list]]:
    prev = [0, 0, 0, 0]
    ws: list[dict[str, Any]] = []
    tl: dict[str, list[dict[str, Any]]] = {}
    for i, step in enumerate(sc["steps"]):
        log, event, expect = step["log"], step["event"], step["expect"]
        where = f"{sc['name']} step {i} ({step.get('note', event['type'])})"
        if validate:
            kind = "workspace" if log == WORKSPACE else "ticket"
            if step.get("schema_refused"):  # the schema is the first line; the model must refuse it as well
                with pytest.raises(schema.SchemaError):
                    schema.validate("event." + event["type"], event, log=kind)
            else:
                schema.validate("event." + event["type"], event, log=kind)
        st = state_of(sc, ws, tl)
        if step.get("schema_refused"):
            check_append_code(sc, st, step, where)
        if step.get("replay_only"):
            assert expect == "ok", where
            unsigned = {k: v for k, v in event.items() if k != "host_sig"}
            assert admit(st, unsigned, log=log).code == Code.CHAIN_BAD_WS_SEQ, where
            (ws if log == WORKSPACE else tl.setdefault(log, [])).append(event)
            now = state_of(sc, ws, tl)
            assert not now.chain_errors, (where, now.chain_errors)
            assert not now._core.logs[log].invalid, (where, now._core.logs[log].invalid)
            if "after" in step:
                check_after(now, step["after"])
            continue
        genesis = event["type"] == "workspace.created"  # checks its own host_sig (§5.11 step 4): admitted signed
        r = admit(st, event if genesis else {k: v for k, v in event.items() if k != "host_sig"}, log=log)
        got = "ok" if not hasattr(r, "code") else r.code.value
        if expect == "refused":  # the spec says refused, no code is pinned
            assert got != "ok", f"{where}: admit accepted an event the vector says is refused"
            continue
        assert got == expect, f"{where}: admit said {got}, the vector says {expect}"
        if expect == "ok":
            (ws if log == WORKSPACE else tl.setdefault(log, [])).append(event)
            now = state_of(sc, ws, tl)
            if "after" in step:
                check_after(now, step["after"])
            if "raised" in step:  # the table row: exactly these gates went up, by one
                uid = log if log != WORKSPACE else next(iter(now.tickets))
                got = _gens(now, uid)
                delta = [
                    g for g, a, b in zip(("requirements", "plan", "verify", "code"), prev, got, strict=True) if a != b
                ]
                assert delta == step["raised"] and all(b - a <= 1 for a, b in zip(prev, got, strict=True)), where
            if now.tickets:
                prev = _gens(now, next(iter(now.tickets)))
            continue
        # replay: the same refused event as the next line of the log is absent for state and reported
        ws2, tl2 = list(ws), {u: list(v) for u, v in tl.items()}
        (ws2 if log == WORKSPACE else tl2.setdefault(log, [])).append(copy.deepcopy(event))
        st2 = state_of(sc, ws2, tl2)
        if step.get("replay_expect") == "chain.broken":
            assert [(c.log, c.seq, c.code) for c in st2.chain_errors] == [(log, event["seq"], "chain.broken")], where
        elif Code(expect) in CHAIN_CODES or expect in F1_CHAIN_BREAKS:
            assert any(c.log == log for c in st2.chain_errors), where
        elif log == WORKSPACE:
            bad = [x for x in st2.workspace.invalid if x.seq == event["seq"]]
            assert bad and bad[0].code == expect, f"{where}: replay recorded {[x.code for x in st2.workspace.invalid]}"
        else:
            inv = st2._core.logs[log].invalid
            assert inv and inv[-1].seq == event["seq"] and inv[-1].code == expect, f"{where}: replay recorded {inv}"
    final = state_of(sc, ws, tl)
    assert not final.chain_errors, final.chain_errors
    assert not final.workspace.invalid, final.workspace.invalid
    assert not any(t.frozen for t in final.tickets.values())
    return ws, tl
