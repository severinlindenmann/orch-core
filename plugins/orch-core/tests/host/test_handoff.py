"""H5b, the hand-off (§0.8.1 "Hand-off (F5)", R6, R8): codes, ``human.pending {code}`` with its per-connection lookup
limit, binding in ``human.get``, and ``orch sign CODE`` over the workspace socket."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from orch.host import handoff, signer
from orch.host import human as host_human
from orch.host.client import Connection, Pin
from orch.host.control_client import new_idem
from orch.host.errors import HostError
from tests.host import human_helpers
from tests.host.human_helpers import KeyBackend, err, ok
from tests.host.lifecycle_helpers import UID
from tests.host.test_human import logged, record, requests_dir, tick
from tests.host.test_human_fixes import _owner_device

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}")


def connect(served) -> Connection:
    return Connection(Pin(UID, served.wid, served.lab.run / served.wid / "host.sock"), client="test sign")


def by_code(c, code: str) -> list[dict]:
    return ok(c.req("human.pending", {"code": code}))["data"]["requests"]


def by_id(c, rid: str) -> dict:
    (item,) = ok(c.req("human.pending", {"request_id": rid}))["data"]["requests"]
    return item


class Shown:
    """The person's terminal: what the review showed, and the word typed (the right one unless told otherwise)."""

    def __init__(self, answer: bool = True) -> None:
        self.texts: list[str] = []
        self.words: list[str] = []
        self.secrets: list[str] = []
        self.answer = answer

    def confirm(self, text: str, word: str) -> bool:
        self.texts.append(text)
        self.words.append(word)
        return self.answer

    def secret(self, text: str) -> None:
        self.secrets.append(text)


def sign(served, human, code: str, *, shown: Shown | None = None, device: str | None = None, backend=None, **kw):
    shown = shown or Shown()
    with connect(served) as conn:
        return handoff.sign_code(
            conn.call,
            code,
            backend=backend or human.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=human.person.ref,
            device=device or human.person.device,
            workspace=kw.pop("workspace", lambda w: ("Acme", "DEMO") if w == served.wid else None),
            confirm=shown.confirm,
            show_secret=shown.secret,
            idem=kw.pop("idem", new_idem()),
        )


# --- codes ----------------------------------------------------------------------------------------------------------


def test_a_handoff_request_gets_a_code_and_a_signer_request_none(human):
    _, key = human.ticket()
    codes = set()
    for i in range(5):
        p = human.prepare("log", {"ref": key, "text": f"h{i}"}, handoff=True)
        assert CODE.fullmatch(p["request"]["code"]), p["request"]
        codes.add(p["request"]["code"])
    assert len(codes) == 5
    assert human.prepare("log", {"ref": key, "text": "s"})["request"]["code"] is None  # bound at prepare: no code
    assert human.prepare("log", {"ref": key, "text": "d"}, handoff=True, dry_run=True)["request"] is None


def test_a_code_is_never_issued_twice_in_a_workspace(human, served, monkeypatch):
    _, key = human.ticket()
    first = human.prepare("log", {"ref": key, "text": "a"}, handoff=True)["request"]["code"]
    ok(human.c.req("human.cancel", {"request_id": human.prepare("log", {"ref": key, "text": "b"})["request"]["id"]}))
    drawn = iter([first, first, "ZZZZ-0000"])  # the CSPRNG repeats a code still stored: it draws again
    monkeypatch.setattr(host_human, "new_code", lambda: next(drawn))
    assert human.prepare("log", {"ref": key, "text": "c"}, handoff=True)["request"]["code"] == "ZZZZ-0000"


def test_the_code_alphabet_is_crockford_base32_with_40_bits():
    assert len(host_human.CODE_ALPHABET) == 32 and not set("ILOU") & set(host_human.CODE_ALPHABET)
    assert all(CODE.fullmatch(host_human.new_code()) for _ in range(200))
    assert handoff.normalise_code(" 7f3k q2ma ".replace(" ", "")) == "7F3K-Q2MA"
    for bad in ("7F3K-Q2M", "7F3K-Q2MAX", "7F3K-Q2MU", "ILOU-ILOU"):
        with pytest.raises(HostError):
            handoff.normalise_code(bad)


def test_a_code_resolves_only_for_the_same_principal_and_person_and_only_while_pending(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    code, rid = p["request"]["code"], p["request"]["id"]
    assert [r["id"] for r in by_code(human.c, code)] == [rid]
    path = requests_dir(served) / rid
    rec = record(served, rid)
    for field, value in (("principal", "uid:999999"), ("person", "p_" + "f" * 32)):
        path.write_text(json.dumps({**rec, field: value}))  # another principal's or person's request
        assert by_code(served.client(), code) == []
    path.write_text(json.dumps(rec))
    assert len(by_code(served.client(), code)) == 1
    tick(served, 601)  # the code expires with the request
    assert by_code(served.client(), code) == []


# --- acceptance: a successful hand-off sign end to end ---------------------------------------------------------------


def test_a_handoff_is_signed_end_to_end_with_orch_sign(human, served):
    _, key = human.ticket()
    preparer = served.client()  # the dashboard: prepares, shows the code, never signs
    p = ok(
        preparer.req(
            "human.prepare", {"op": "log", "args": {"ref": key, "text": "from the dashboard"}, "handoff": True}
        )
    )["data"]
    code, rid = p["request"]["code"], p["request"]["id"]
    shown = Shown()
    result = sign(served, human, code, shown=shown)
    assert result["seq"] >= 1 and result["key"] == key
    ev = logged(served, key)[-1]
    assert ev["id"] == p["events"][0]["id"] and ev["text"] == "from the dashboard"
    assert ev["actor"]["device"] == human.person.device and ev["auth"] == "passphrase"
    assert shown.words == [f"{key}/{served.lab.ws.uid(key)[-8:]}"]
    assert len(human.backend.payloads) == 1  # one passphrase prompt, for the one event
    item = by_id(preparer, rid)  # the dashboard learns the outcome (R6)
    assert item["state"] == "submitted" and item["seq"] == result["seq"] and item["code"] == code
    assert by_code(preparer, code) == []  # single use: a submitted request's code names nothing any more
    with pytest.raises(HostError) as e:
        sign(served, human, code)
    assert e.value.code == "not_found"


def test_a_handoff_grant_shows_its_secret_only_after_the_commit(human, served):
    p = human.prepare("grant", {"hours": 1}, handoff=True)
    shown = Shown()
    sign(served, human, p["request"]["code"], shown=shown)
    assert shown.words == ["GRANT"] and len(shown.secrets) == 1 and "ORCH_GRANT=gr_" in shown.secrets[0]
    assert "ORCH_GRANT" not in "".join(shown.texts)


# --- acceptance: the header from the signer's own registry, before content -------------------------------------------


def test_orch_sign_shows_the_notice_then_the_header_then_content(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "the body"}, handoff=True)
    shown = Shown()
    sign(served, human, p["request"]["code"], shown=shown, workspace=lambda w: ("Mine", "MINE"))
    lines = shown.texts[0].splitlines()
    assert lines[0] == handoff.NOTICE and "only sign a code you created yourself" in lines[0]
    ws_line = next(i for i, x in enumerate(lines) if x.startswith("workspace: "))
    op_line = next(i for i, x in enumerate(lines) if x.startswith("op: "))
    fp_line = next(i for i, x in enumerate(lines) if x.startswith("fingerprint: "))
    content = next(i for i, x in enumerate(lines) if "the body" in x)
    assert ws_line < op_line < fp_line < content
    assert lines[ws_line] == f"workspace: Mine (MINE) {served.wid}"  # the signer's registry, never host text
    assert lines[op_line].startswith("op: log: ")
    assert lines[fp_line] == f"fingerprint: {p['fingerprint']}"  # the one the dashboard shows (R8)


def test_orch_sign_refuses_a_workspace_outside_its_registry_before_any_prompt(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    shown = Shown()
    with pytest.raises(signer.SignerRefused, match="own registry"):
        sign(served, human, p["request"]["code"], shown=shown, workspace=lambda w: None)
    assert shown.texts == [] and human.backend.payloads == []
    assert by_id(human.c, p["request"]["id"])["state"] == "cancelled"  # the preparer learns it


def test_a_declined_review_signs_nothing_and_cancels(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    with pytest.raises(signer.SignerRefused, match="not confirmed"):
        sign(served, human, p["request"]["code"], shown=Shown(answer=False))
    assert human.backend.payloads == [] and by_id(human.c, p["request"]["id"])["state"] == "cancelled"


class CliBackend(KeyBackend):
    def public_key(self, key_id: str) -> bytes:
        return self.person.sig_pub


def test_the_orch_sign_command_end_to_end(human, served, monkeypatch):
    from orch.cli.main import main
    from orch.ops import human as ops_human

    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "via the command"}, handoff=True)
    keys = ops_human.keys_dir(served.lab.state, served.wid)
    keys.mkdir(parents=True, exist_ok=True)
    (keys / ops_human.KEY_FILE).touch()  # the key file is only looked at; the backend is the test's
    backend = CliBackend(human.person)
    shown = Shown()
    monkeypatch.setattr(ops_human, "open_backend", lambda d: backend)
    monkeypatch.setattr(ops_human, "review_prompt", shown.confirm)
    monkeypatch.setattr(ops_human, "show_secret", shown.secret)
    out, errs = io.StringIO(), io.StringIO()
    argv = ["sign", p["request"]["code"].lower(), "--state", str(served.lab.state), "--run", str(served.lab.run)]
    code = main([*argv, "--json"], env={"HOME": str(served.lab.tmp)}, stdout=out, stderr=errs)
    assert code == 0, out.getvalue()
    assert json.loads(out.getvalue())["result"]["key"] == key
    assert handoff.NOTICE in errs.getvalue()  # every time it runs
    text = shown.texts[0]
    name = Path(served.lab.root).name  # from the person's own registry (the root's name and the prefix)
    ws_line = next(x for x in text.splitlines() if x.startswith("workspace: "))
    assert ws_line == f"workspace: {name} ({served.service.entry.prefix}) {served.wid}"
    assert logged(served, key)[-1]["text"] == "via the command"
    # an agent never signs: ORCH_GRANT set is refused before anything is looked up
    out = io.StringIO()
    assert main([*argv, "--json"], env={"ORCH_GRANT": "x"}, stdout=out, stderr=io.StringIO()) != 0
    assert json.loads(out.getvalue())["error"]["code"] == "human_only"


# --- acceptance: binding to a second device is refused ---------------------------------------------------------------


def test_binding_to_a_second_device_is_refused(human, served, lab):
    second = _owner_device(lab, ("look", "decide", "operate", "type"))  # a valid device of the same person
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    rid, code = p["request"]["id"], p["request"]["code"]
    bound = ok(human.get(rid, signer={"device": second, "auth": "passphrase"}))["data"]
    assert bound["events"][0]["actor"]["device"] == second
    # orch sign on this machine (the first device) finds the code, but the request is bound to the other device
    with pytest.raises(HostError) as e:
        sign(served, human, code)
    assert e.value.code == "human.request_state"
    assert human.backend.payloads == []
    assert record(served, rid)["signer"]["device"] == second
    # and the other way round: bound here first, the second device is refused
    p2 = human.prepare("log", {"ref": key, "text": "y"}, handoff=True)
    ok(human.get(p2["request"]["id"], signer=human.signer))
    assert err(human.get(p2["request"]["id"], signer={"device": second, "auth": "passphrase"})) == "human.request_state"
    assert err(human.get(p2["request"]["id"], signer={**human.signer, "auth": "secure-enclave"})) == (
        "human.request_state"
    )


# --- acceptance: failed lookups on one connection do not lock out another (F5) -------------------------------------


def test_failed_lookups_on_one_connection_do_not_lock_out_another(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    code = p["request"]["code"]
    guesser = served.client()
    for guess in ("0000-0000", "0000-0001", "0000-0002", "0000-0003", "0000-0004"):
        assert by_code(guesser, guess) == []
    res = guesser.req("human.pending", {"code": code})  # limited: even the right code, on this connection
    assert err(res) == "rate.limited" and res["error"]["retryable"] is True
    assert 0 < res["error"]["retry_after_ms"] <= 60_000
    assert ok(guesser.req("human.pending", {"request_id": p["request"]["id"]}))  # ids are not limited
    other = served.client()
    assert [r["id"] for r in by_code(other, code)] == [p["request"]["id"]]  # another connection is not locked out
    for guess in ("0000-0005", "0000-0006", "0000-0007", "0000-0008"):  # successes are not failures
        assert by_code(other, guess) == []
    assert by_code(other, code) and by_code(other, code)
    tick(served, 60)  # the window passes: the guesser may look up again
    assert [r["id"] for r in by_code(guesser, code)] == [p["request"]["id"]]


# --- acceptance: the pending states prepared, submitted, cancelled and expired (R6) -------------------------------


def test_pending_states_prepared_submitted_cancelled_expired(human, served):
    _, key = human.ticket()
    c = served.client()

    def handed(text: str) -> dict:
        return human.prepare("log", {"ref": key, "text": text}, handoff=True)

    prepared = handed("p")
    item = by_id(c, prepared["request"]["id"])
    assert item["state"] == "prepared" and item["seq"] is None and item["code"] == prepared["request"]["code"]
    assert item["event_ids"] == [prepared["events"][0]["id"]] and item["key"] == key and item["op"] == "log"
    assert by_code(c, prepared["request"]["code"]) == [item]

    submitted = handed("s")
    result = sign(served, human, submitted["request"]["code"])
    item = by_id(c, submitted["request"]["id"])
    assert item["state"] == "submitted" and item["seq"] == result["seq"]
    assert item["event_ids"] == [submitted["events"][0]["id"]] and "error" not in item

    cancelled = handed("c")
    ok(c.req("human.cancel", {"request_id": cancelled["request"]["id"]}))
    item = by_id(c, cancelled["request"]["id"])
    assert item["state"] == "cancelled" and item["seq"] is None
    assert by_code(c, cancelled["request"]["code"]) == []

    expired = handed("e")
    tick(served, 601)
    item = by_id(c, expired["request"]["id"])
    assert item["state"] == "expired" and item["seq"] is None
    assert by_id(c, prepared["request"]["id"])["state"] == "expired"
    assert by_id(c, submitted["request"]["id"])["state"] == "submitted"  # terminal states stay
