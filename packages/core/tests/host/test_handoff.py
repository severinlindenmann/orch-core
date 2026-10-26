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
        self.texts: list[str] = []  # the full review (the details behind `d`); `plain` is what is shown first
        self.plain: list[str] = []
        self.words: list[str] = []
        self.secrets: list[str] = []
        self.answer = answer

    def confirm(self, text: str, word: str) -> bool:
        self.texts.append(getattr(text, "details", "") or text)
        self.plain.append(str(text))
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
            notice=kw.pop("notice", True),
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
    live = human.prepare("log", {"ref": key, "text": "a"}, handoff=True)["request"]["code"]
    ended = human.prepare("log", {"ref": key, "text": "b"}, handoff=True)["request"]
    ok(human.c.req("human.cancel", {"request_id": ended["id"]}))  # terminal, still stored (24 h)
    signed = human.prepare("log", {"ref": key, "text": "c"}, handoff=True)["request"]["code"]
    sign(served, human, signed)  # submitted, still stored
    drawn = iter([ended["code"], signed, live, "ZZZZ-0000"])  # the CSPRNG repeats stored codes: it draws again
    monkeypatch.setattr(host_human, "new_code", lambda: next(drawn))
    assert human.prepare("log", {"ref": key, "text": "d"}, handoff=True)["request"]["code"] == "ZZZZ-0000"


def test_the_code_alphabet_is_crockford_base32_with_40_bits():
    assert len(host_human.CODE_ALPHABET) == 32 and not set("ILOU") & set(host_human.CODE_ALPHABET)
    assert all(CODE.fullmatch(host_human.new_code()) for _ in range(200))
    for good in ("  7f3kq2ma ", "7F3K-Q2MA", " 7f3k-q2ma\n"):
        assert handoff.normalise_code(good) == "7F3K-Q2MA"
    bad_shapes = ("7-F3KQ2MA", "7F3KQ-2MA", "7F3K--Q2MA", "7F3K Q2MA", "7F3K-Q2MA-", "-7F3KQ2MA")
    for bad in ("7F3K-Q2M", "7F3K-Q2MAX", "7F3K-Q2MU", "ILOU-ILOU", *bad_shapes):
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


def test_the_first_view_of_orch_sign_prints_the_fingerprint_the_dashboard_shows(human, served):
    """A-3: the dashboard says "check that this matches the fingerprint in your terminal". What the person reads first
    (the plain view, before `d`) carries the same 12 hex on a labelled line under the headline, before any content."""
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "the body"}, handoff=True)
    shown = Shown()
    sign(served, human, p["request"]["code"], shown=shown)
    first = shown.plain[0].splitlines()
    at = first.index(f"Verification code: {p['fingerprint']}")  # headline, "in workspace ...", fingerprint
    assert first[at - 1].startswith("in workspace ") and at < next(i for i, x in enumerate(first) if "the body" in x)
    assert (
        len(p["fingerprint"]) == 12 and f"fingerprint: {p['fingerprint']}" in shown.texts[0]
    )  # the details keep theirs


def test_the_warning_is_not_repeated_in_the_review_when_stderr_is_the_terminal(human, served):
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "the body"}, handoff=True)
    shown = Shown()
    sign(served, human, p["request"]["code"], shown=shown, notice=False)  # N13: main() passes this for a tty stderr
    assert "only sign a code you created yourself" not in shown.texts[0] and "the body" in shown.texts[0]


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
    assert [r["id"] for r in by_code(other, code)] == [p["request"]["id"]]  # 4 failures and a success: not limited
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


# --- the command's search, files and failures (review round) -------------------------------------------------------


class Cli:
    """``orch sign`` as the person runs it, with the test person's key and terminal."""

    def __init__(self, human, served, monkeypatch, backend=None) -> None:
        from orch.ops import human as ops_human

        keys = ops_human.keys_dir(served.lab.state, served.wid)
        keys.mkdir(parents=True, exist_ok=True)
        (keys / ops_human.KEY_FILE).touch()  # the key file is only looked at; the backend is the test's
        self.backend = backend or CliBackend(human.person)
        self.shown = Shown()
        monkeypatch.setattr(ops_human, "open_backend", lambda d: self.backend)
        monkeypatch.setattr(ops_human, "review_prompt", self.shown.confirm)
        monkeypatch.setattr(ops_human, "show_secret", self.shown.secret)
        self.served = served

    def entry(self) -> dict:
        (entry,) = handoff._own_workspaces(self.served.lab.state, UID, None)
        return entry

    def __call__(self, code: str, *extra: str, state=None) -> tuple[int, dict, str]:
        from orch.cli.main import main

        out, errs = io.StringIO(), io.StringIO()
        argv = ["sign", code, "--state", str(state or self.served.lab.state), "--run", str(self.served.lab.run)]
        rc = main([*argv, *extra, "--json"], env={"HOME": str(self.served.lab.tmp)}, stdout=out, stderr=errs)
        return rc, json.loads(out.getvalue()), errs.getvalue()


class Track:
    """Every ``Connection`` the command opens, and which of them it closed; ``fail(n)`` makes the n-th one's calls
    raise ``workspace.unavailable``."""

    def __init__(self, monkeypatch) -> None:
        self.opened: list = []
        self.closed: list = []
        self.failing: set[int] = set()
        real_init, real_call, real_close = Connection.__init__, Connection.call, Connection.close
        track = self

        def init(self, *a, **kw):
            real_init(self, *a, **kw)
            track.opened.append(self)

        def call(self, op, args, **kw):
            if track.opened.index(self) in track.failing:
                raise HostError("workspace.unavailable", "down for the test")
            return real_call(self, op, args, **kw)

        def close(self):
            track.closed.append(self)
            real_close(self)

        monkeypatch.setattr(Connection, "__init__", init)
        monkeypatch.setattr(Connection, "call", call)
        monkeypatch.setattr(Connection, "close", close)

    def all_closed(self) -> bool:
        return bool(self.opened) and all(c in self.closed for c in self.opened)


def _artifact(human, **kw) -> tuple[str, dict, bytes]:
    import base64

    _, key = human.ticket()
    data = b"the proof bytes\n"
    p = human.prepare(
        "artifact.add",
        {"ref": key, "path": "proof.txt"},
        files={"path": {"b64": base64.b64encode(data).decode()}},
        **kw,
    )
    return key, p, data


def _line(text: str, start: str) -> str:
    return next(x for x in text.splitlines() if x.startswith(start))


def test_a_handoff_review_labels_artifact_digests_it_has_not_seen(human, served):
    key, p, _ = _artifact(human, handoff=True)
    shown = Shown()
    sign(served, human, p["request"]["code"], shown=shown)
    text = shown.texts[0]
    for field in ("sha256: ", "bytes: "):
        assert _line(text, field).endswith(f"({signer.UNSEEN})"), _line(text, field)
    assert "signed commitment: orch sign has not seen these bytes" in signer.UNSEEN
    assert logged(served, key)[-1]["type"] == "artifact.added"


def test_the_composite_review_of_an_artifact_is_unchanged(human, served):
    import base64

    _, key = human.ticket()
    data = b"the proof bytes\n"
    files = {"path": {"b64": base64.b64encode(data).decode()}}
    shown = Shown()

    def call(op, args, files=None, idem=None):
        return ok(human.c.req(op, args, files=files, idem=idem))

    signer.run(
        call,
        "artifact.add",
        {"ref": key, "path": "proof.txt"},
        backend=human.backend,
        key_id="dk",
        workspace_id=served.wid,
        person=human.person.ref,
        device=human.person.device,
        workspace=lambda w: ("Acme", "DEMO"),
        confirm=shown.confirm,
        show_secret=shown.secret,
        idem=new_idem(),
        files=files,
    )
    text = shown.texts[0]
    assert _line(text, "sha256: ").startswith("sha256: sha256:") and "(" not in _line(text, "sha256: ")
    assert _line(text, "bytes: ") == f"bytes: {len(data)}"
    assert signer.UNSEEN not in text and signer.SEEN not in text


def test_orch_sign_file_checks_the_artifact_against_a_local_file(human, served, monkeypatch, tmp_path):
    cli = Cli(human, served, monkeypatch)
    key, p, data = _artifact(human, handoff=True)
    good = tmp_path / "proof.txt"
    good.write_bytes(data)
    rc, doc, _ = cli(p["request"]["code"], "--file", f"proof.txt={good}")
    assert rc == 0, doc
    text = cli.shown.texts[0]
    assert _line(text, "sha256: ").endswith(f"({signer.SEEN})") and signer.UNSEEN not in text
    assert logged(served, key)[-1]["type"] == "artifact.added"


def test_orch_sign_file_refuses_another_file_or_a_wrong_name_and_cancels(human, served, monkeypatch, tmp_path):
    cli = Cli(human, served, monkeypatch)
    bad = tmp_path / "other.txt"
    bad.write_bytes(b"not the proof\n")
    for extra, why in (
        (("--file", f"proof.txt={bad}"), "not your local file"),
        (("--file", f"nope.txt={bad}"), "no artifact of that name"),
    ):
        _, p, _ = _artifact(human, handoff=True)
        rc, doc, _ = cli(p["request"]["code"], *extra)
        assert rc != 0 and doc["error"]["code"] == "invalid.input" and why in doc["error"]["message"], doc
        assert by_id(human.c, p["request"]["id"])["state"] == "cancelled"
    assert cli.shown.texts == [] and cli.backend.payloads == []
    rc, doc, _ = cli(p["request"]["code"], "--file", "proof.txt")
    assert doc["error"]["code"] == "invalid.input" and "NAME=PATH" in doc["error"]["message"]


def test_a_pin_failure_is_a_named_refusal_not_a_missing_workspace(human, served, monkeypatch):
    cli = Cli(human, served, monkeypatch)
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    wrong_uid = {**cli.entry(), "host_uid": UID + 1}  # the socket's process is not the registry's host UID
    wrong_ws = {**cli.entry(), "workspace_id": "f" * 32}
    for entry in (wrong_uid, wrong_ws):
        if entry is wrong_ws:  # the same socket answers, but its welcome names another workspace
            link = served.lab.run / ("f" * 32)
            link.mkdir(exist_ok=True)
            (link / "host.sock").symlink_to(served.lab.run / served.wid / "host.sock")
        monkeypatch.setattr(handoff, "_own_workspaces", lambda *a, entry=entry: [entry])
        rc, doc, _ = cli(p["request"]["code"])
        assert rc != 0 and doc["error"]["code"] == "host.untrusted", doc
    assert by_id(human.c, p["request"]["id"])["state"] == "prepared"  # nothing was asked of the untrusted socket


def test_one_workspace_failing_does_not_stop_the_search(human, served, monkeypatch):
    cli = Cli(human, served, monkeypatch)
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    entry = cli.entry()
    absent = {**entry, "workspace_id": "e" * 32}  # registered but not served: skipped
    monkeypatch.setattr(handoff, "_own_workspaces", lambda *a: [absent, entry, entry])
    track = Track(monkeypatch)
    track.failing = {0}  # the first one asked answers workspace.unavailable
    rc, doc, errs = cli(p["request"]["code"])
    assert rc == 0, doc
    assert "not asked:" in errs and "1 other workspace on this computer is not reachable" in errs
    assert "workspace.unavailable" not in errs and entry["workspace_id"] not in errs  # F2: plain words, no ids
    assert track.all_closed() and len(track.opened) == 2  # the failing connection was closed (33625455)


def test_when_no_workspace_holds_the_code_the_failures_are_reported(human, served, monkeypatch):
    cli = Cli(human, served, monkeypatch)
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    track = Track(monkeypatch)
    track.failing = {0}
    rc, doc, _ = cli(p["request"]["code"])
    assert rc != 0 and doc["error"]["code"] == "not_found" and "not reachable" in doc["error"]["message"]
    assert track.all_closed()
    assert by_id(human.c, p["request"]["id"])["state"] == "prepared"  # fails closed: nothing bound or signed


def test_a_code_in_several_workspaces_is_refused_and_every_connection_closed(human, served, monkeypatch):
    cli = Cli(human, served, monkeypatch)
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    entry = cli.entry()
    monkeypatch.setattr(handoff, "_own_workspaces", lambda *a: [entry, {**entry}])
    track = Track(monkeypatch)
    rc, doc, _ = cli(p["request"]["code"])
    assert doc["error"]["code"] == "invalid.input" and "--workspace" in doc["error"]["message"]
    assert track.all_closed() and len(track.opened) == 2
    assert by_id(human.c, p["request"]["id"])["state"] == "prepared" and cli.shown.texts == []


def test_workspace_filter_and_no_person_mapping(human, served, monkeypatch, tmp_path):
    from orch.host.custody import ensure_private_dir, write_private
    from orch.host.registry import registry_path

    cli = Cli(human, served, monkeypatch)
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    prefix = served.service.entry.prefix
    rc, doc, _ = cli(p["request"]["code"], "--workspace", "NOPE")
    assert doc["error"]["code"] == "not_found" and "maps you to a person" in doc["error"]["message"]
    rc, doc, _ = cli(p["request"]["code"], "--workspace", prefix)
    assert rc == 0, doc
    # a registry of the caller's own: only entries mapping this UID to a person count, filtered by id or prefix
    state = tmp_path / "st"
    ensure_private_dir(state, UID)
    ensure_private_dir(state / "host", UID)

    def ws(wid: str, prefix: str, peer: dict) -> dict:
        return {
            "workspace_id": wid,
            "prefix": prefix,
            "root": f"/x/{prefix}",
            "host_uid": UID,
            "peers": {str(UID): peer},
        }

    write_private(
        registry_path(state),
        {
            "workspaces": [
                ws("a" * 32, "AAA", {"class": "person", "person": "p_" + "1" * 32}),
                ws("b" * 32, "BBB", {"class": "agent"}),
                ws("c" * 32, "CCC", {"class": "person", "person": "p_" + "1" * 32}),
                {**ws("d" * 32, "DDD", {}), "peers": {str(UID + 1): {"class": "person", "person": "p_" + "2" * 32}}},
            ]
        },
    )
    assert [e["prefix"] for e in handoff._own_workspaces(state, UID, None)] == ["AAA", "CCC"]
    assert [e["prefix"] for e in handoff._own_workspaces(state, UID, "CCC")] == ["CCC"]
    assert [e["prefix"] for e in handoff._own_workspaces(state, UID, "a" * 32)] == ["AAA"]
    assert handoff._own_workspaces(state, UID, "BBB") == []  # mapped as an agent: not the person's
    rc, doc, _ = cli(p["request"]["code"], "--workspace", "DDD", state=state)
    assert doc["error"]["code"] == "not_found" and "maps you to a person" in doc["error"]["message"]


def test_a_check_failing_after_the_bind_cancels(human, served):
    from tests.identity.helpers import Person

    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)

    def call(op, args, idem=None, files=None):
        res = ok(human.c.req(op, args, idem=idem, files=files))
        if op == "human.get":
            res["data"]["events"][0]["actor"]["id"] = Person().ref  # a host bug after the bind
        return res

    shown = Shown()
    with pytest.raises(signer.SignerRefused, match="not for this signer's person"):
        handoff.sign_code(
            call,
            p["request"]["code"],
            backend=human.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=human.person.ref,
            device=human.person.device,
            workspace=lambda w: ("Acme", "DEMO"),
            confirm=shown.confirm,
            show_secret=shown.secret,
            idem=new_idem(),
        )
    assert shown.texts == [] and human.backend.payloads == []
    assert by_id(human.c, p["request"]["id"])["state"] == "cancelled"


def test_a_failing_passphrase_cancels_and_signs_nothing(human, served):
    from orch.custody.base import WrongPassphrase

    class Wrong(KeyBackend):
        def sign(self, key_id, payload, *, action):
            raise WrongPassphrase("wrong passphrase")

    _, key = human.ticket()
    before = len(logged(served, key))
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    shown = Shown()
    with pytest.raises(WrongPassphrase):
        sign(served, human, p["request"]["code"], shown=shown, backend=Wrong(human.person))
    assert len(shown.texts) == 1  # the review was shown, then the passphrase failed
    assert by_id(human.c, p["request"]["id"])["state"] == "cancelled"
    assert len(logged(served, key)) == before


def test_ctrl_c_at_the_passphrase_prompt_is_a_calm_cancel_and_cancels_the_request(human, served, monkeypatch):
    class CtrlC(CliBackend):
        def sign(self, key_id, payload, *, action):
            raise KeyboardInterrupt  # what the passphrase prompt raises on Ctrl-C

    backend = CtrlC(human.person)
    cli = Cli(human, served, monkeypatch, backend=backend)
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    rc, doc, _ = cli(p["request"]["code"])
    assert rc != 0 and doc["error"]["code"] == "invalid.input", doc
    assert doc["error"]["message"] == "Cancelled, nothing was signed."
    out = io.StringIO()  # the human line carries no error-code prefix; the exit code stays
    assert handoff._fail(out, False, HostError("invalid.input", "Cancelled, nothing was signed."), plain=True) == rc
    assert out.getvalue() == "orch sign: Cancelled, nothing was signed.\n"
    assert by_id(human.c, p["request"]["id"])["state"] == "cancelled"
    assert backend.payloads == [] and logged(served, key)[-1].get("text") != "x"


def test_ctrl_c_after_the_signature_does_not_claim_that_nothing_was_signed(human, served, monkeypatch):
    cli = Cli(human, served, monkeypatch)
    _, key = human.ticket()
    p = human.prepare("log", {"ref": key, "text": "x"}, handoff=True)
    real = Connection.call

    def call(self, op, args, **kw):
        if op == "human.submit":
            raise KeyboardInterrupt  # the signature exists; whether the host committed is unknown
        return real(self, op, args, **kw)

    monkeypatch.setattr(Connection, "call", call)
    rc, doc, _ = cli(p["request"]["code"])
    assert rc != 0 and doc["error"]["code"] == "invalid.input", doc
    assert "nothing was signed" not in doc["error"]["message"]
    assert doc["error"]["message"].startswith("Interrupted;")
    assert cli.backend.payloads  # it was signed


def test_a_key_in_one_click_custody_with_click_off_is_told_plainly_and_nothing_is_signed():
    """F1: the key stays in one-click custody after `convenience off`; the host refuses the binding (`disabled`). The
    person gets the two ways out, and the request keeps its method (no silent signing with another one)."""
    import pytest

    class Convenience:
        auth = "convenience"

    class Passphrase:
        auth = "passphrase"

    def refuse(*a, backend, **kw):
        raise HostError(
            "sign.convenience_unavailable", "disabled: convenience signing is off", detail={"reason": "disabled"}
        )

    entry = {"workspace_id": "a" * 32, "prefix": "ORCHI"}
    with pytest.raises(HostError) as caught:
        handoff._sign_code_explained(entry, refuse, backend=Convenience())
    assert "one-click custody" in caught.value.message and "ORCHI" in (caught.value.hint or "")
    assert "orch keys custody passphrase" in (caught.value.hint or "")
    with pytest.raises(HostError) as other:  # a passphrase key never gets this rewrite
        handoff._sign_code_explained(entry, refuse, backend=Passphrase())
    assert other.value.message.startswith("disabled: convenience signing is off")


def test_orch_sign_says_the_key_has_no_passphrase_only_for_a_convenience_key(human, served, monkeypatch):
    """#1170 A3: with One click on, `orch sign` works and tells the person what kind of key signs."""
    from orch.host import convenience

    class ConvenienceKey(CliBackend):
        auth = "convenience"

    _, key = human.ticket()
    cli = Cli(human, served, monkeypatch, backend=ConvenienceKey(human.person, "convenience"))
    convenience.set_enabled(served.lab.state, served.wid, True, UID)
    code = human.prepare("log", {"ref": key, "text": "one click"}, handoff=True)["request"]["code"]
    rc, doc, errs = cli(code)
    assert rc == 0, doc
    assert handoff.NO_PASSPHRASE in errs
    assert handoff.NO_PASSPHRASE in cli.shown.texts[-1]  # stderr is not the terminal here: the review carries it
    assert served.lab.ws.events(key)[-1]["auth"] == "convenience"
    plain = Cli(human, served, monkeypatch)
    code = human.prepare("log", {"ref": key, "text": "passphrase"}, handoff=True)["request"]["code"]
    convenience.set_enabled(served.lab.state, served.wid, False, UID)
    rc, doc, errs = plain(code)
    assert rc == 0 and handoff.NO_PASSPHRASE not in errs
    assert handoff.NO_PASSPHRASE not in plain.shown.texts[-1]
