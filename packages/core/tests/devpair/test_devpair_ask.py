"""dev-pair ``ask`` / ``check-answer``: the pure parts (nonce, session id, grant parsing, log scanning, who is the
phone) and the argument parser. The real-orch run is the slow smoke."""

from __future__ import annotations

import json
import re

import pytest

N = "ab12cd34"
OWNER, PRIMARY, PHONE = "d_" + "1" * 32, "d_" + "2" * 32, "d_" + "3" * 32


def ev(type_, **kw):
    return {"type": type_, **kw}


def person(device):
    return {"kind": "person", "id": "p_" + "a" * 32, "device": device}


def asked(text=f"e2e {N}: type the answer e2e-ok-{N}", qid="Q1"):
    return ev("question.asked", question={"id": qid, "text": text, "blocking": True},
              actor={"kind": "agent", "id": "orch-cli"})  # fmt: skip


def answered(device=PHONE, option=f"e2e_ok_{N}", qid="Q1", text=None):
    extra = {} if text is None else {"text": text}
    return ev(
        "question.answered", question=qid, option=option, actor=person(device), at="2026-10-16T10:00:00Z", **extra
    )


WS = [
    ev("workspace.created", actor=person(OWNER)),
    ev("device.added", device=PRIMARY),
    ev("device.added", device=PHONE),
]


@pytest.mark.parametrize("n", ["ab12cd34", "x" * 32])
def test_nonce_accepts(dp, n):
    assert dp.check_nonce(n) == n


@pytest.mark.parametrize("n", ["", "A1b2cd", "abc", "a b c d", "x" * 33, "a;rm", "é" * 5, None, 5])
def test_nonce_refuses(dp, n):
    with pytest.raises(dp.Refused):
        dp.check_nonce(n)


def test_session_id_shape(dp):
    ids = {dp.new_session_id() for _ in range(20)}
    assert len(ids) == 20
    assert all(re.fullmatch(r"s_[0-7][0-9A-HJKMNP-TV-Z]{25}", i) for i in ids)


def test_parse_grant(dp):
    out = b"Grant created.\r\n\x1b[1mORCH_GRANT=gs_abc-DEF_123\x1b[0m\r\nexport it\r\n"
    assert dp.parse_grant(out) == "gs_abc-DEF_123"
    assert dp.parse_grant(bytearray(out)) == "gs_abc-DEF_123"
    assert dp.parse_grant(b"nothing here") is None


def test_asked_text_holds_the_nonce(dp):
    assert N in dp.asked_text(N)


def test_devices_exclude_owner_and_primary(dp):
    assert dp.paired_devices(WS, [PRIMARY, OWNER]) == {PHONE}
    assert dp.paired_devices([], [PRIMARY]) == set()


def test_find_question_by_nonce(dp):
    assert dp.find_question([asked()], N) == "Q1"
    assert dp.find_question([asked(), asked("other", "Q2")], "other") == "Q2"
    assert dp.find_question([asked()], "ffffffff") is None
    assert dp.find_question([asked(qid="bad id")], N) is None


@pytest.mark.parametrize(
    ("event", "found"),
    [
        (answered(), True),
        (answered(device=PRIMARY), False),  # the primary's own signature is not the phone's
        (answered(device=OWNER), False),
        (answered(device="d_" + "9" * 32), False),  # a device that was never added
        (answered(option="e2e_ok_other"), False),
        (answered(option=None, text=f"e2e_ok_{N}"), True),
        (answered(option=None), False),
        (answered(qid="Q2"), False),
        (ev("question.answered", question="Q1", text=f"e2e_ok_{N}", actor={"kind": "agent", "id": "x"}), False),
    ],
)
def test_find_answer(dp, event, found):
    hit = dp.find_answer([asked(), event], "Q1", N, {PHONE})
    assert (hit is not None) is found
    if found:
        assert hit["device"] == PHONE and hit["option"] == f"e2e_ok_{N}"


def write_log(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n{torn")


def test_scan_answer_reads_the_logs(dp, scratch):
    ws = scratch / "ws"
    write_log(ws / "events" / "workspace.jsonl", WS)
    write_log(ws / "tickets" / "T1" / "events.jsonl", [ev("ticket.created", key="SBX-0001"), asked()])
    assert dp.scan_answer(ws, N, PHONE) is None
    write_log(ws / "tickets" / "T1" / "events.jsonl", [ev("ticket.created", key="SBX-0001"), asked(), answered()])
    hit = dp.scan_answer(ws, N, PHONE)
    assert hit["ticket"] == "SBX-0001" and hit["question"] == "Q1" and hit["device"] == PHONE
    assert dp.scan_answer(ws, "ffffffff", PHONE) is None
    # an answer signed by the primary's device is not counted
    write_log(
        ws / "tickets" / "T1" / "events.jsonl", [ev("ticket.created", key="K"), asked(), answered(device=PRIMARY)]
    )
    assert dp.scan_answer(ws, N, PHONE) is None


def test_scan_without_a_workspace_is_none(dp, scratch):
    assert dp.scan_answer(scratch / "nope", N, PHONE) is None


def test_primary_device_id(dp, scratch):
    assert dp.primary_device_id(scratch) == ""
    (scratch / "primary-cert.json").write_text(json.dumps({"o": {"device_id": "f" * 32}, "sig": "x"}))
    assert dp.primary_device_id(scratch) == "d_" + "f" * 32


def test_parser_has_the_new_commands(dp):
    p = dp.build_parser()
    a = p.parse_args(["ask", "--root", "/x", "--nonce", N])
    assert (a.cmd, a.nonce) == ("ask", N)
    c = p.parse_args(["check-answer", "--root", "/x", "--nonce", N, "--timeout", "5"])
    assert (c.cmd, c.timeout) == ("check-answer", 5.0)


def test_main_refuses_a_bad_nonce_and_a_non_sandbox(dp, scratch, capsys):
    assert dp.main(["ask", "--root", str(scratch / "r"), "--nonce", "!"]) == 2
    assert dp.main(["check-answer", "--root", str(scratch / "r"), "--nonce", N, "--timeout", "1"]) == 2
    assert "dev-pair:" in capsys.readouterr().err


def test_check_answer_times_out_with_json(dp, scratch, capsys, monkeypatch):
    monkeypatch.setattr(dp, "_devsandbox", lambda: type("D", (), {"check_manifest": staticmethod(lambda r: {})}))
    monkeypatch.setattr(dp, "check_root_path", lambda r: scratch)  # /tmp is sticky: the real rule is tested elsewhere
    root = scratch
    sandbox_files(root)
    args = dp.build_parser().parse_args(["check-answer", "--root", str(root), "--nonce", N, "--timeout", "0"])
    assert dp.check_answer(args, sleep=lambda s: None) == 1
    assert json.loads(capsys.readouterr().out) == {"answered": False, "nonce": N, "timeout_s": 0.0}


OTHER = "d_" + "4" * 32


def sandbox_files(root, *, primary=True, genesis=True, phone=PHONE, events=None):
    """A sandbox root as `up` leaves it: primary-cert.json, the workspace log, pair.json naming the paired device."""
    if primary:
        (root / "primary-cert.json").write_text(json.dumps({"o": {"device_id": PRIMARY[2:]}, "sig": "x"}))
    evs = events if events is not None else WS
    if not genesis:
        evs = [e for e in evs if e["type"] != "workspace.created"]
    write_log(root / "ws" / "events" / "workspace.jsonl", evs)
    doc = {"offer_id": "o1"} if phone is None else {"offer_id": "o1", "device": phone}
    (root / "pair.json").write_text(json.dumps(doc))


def test_pinned_phone_is_the_offers_device(dp, scratch):
    sandbox_files(scratch)
    assert dp.pinned_phone(scratch) == PHONE


@pytest.mark.parametrize(
    "kw",
    [
        {"primary": False},  # primary-cert.json unreadable: never shrink the exclusion list
        {"genesis": False},  # no workspace.created
        {"phone": None},  # pair.json names no device
        {"phone": PRIMARY},
        {"phone": OWNER},
        {"phone": OTHER},  # named, but never added to the workspace
        {"phone": "not-a-device"},
    ],
)
def test_pinned_phone_fails_closed(dp, scratch, kw):
    sandbox_files(scratch, **kw)
    with pytest.raises(dp.Refused):
        dp.pinned_phone(scratch)


def test_check_answer_refuses_without_the_primary_cert(dp, scratch, monkeypatch, capsys):
    monkeypatch.setattr(dp, "_devsandbox", lambda: type("D", (), {"check_manifest": staticmethod(lambda r: {})}))
    monkeypatch.setattr(dp, "check_root_path", lambda r: scratch)
    sandbox_files(scratch, primary=False)
    args = dp.build_parser().parse_args(["check-answer", "--root", str(scratch), "--nonce", N, "--timeout", "0"])
    assert dp.check_answer(args, sleep=lambda s: None) == 1
    out = capsys.readouterr()
    assert "primary-cert.json" in out.err and out.out == ""


def test_another_added_device_is_not_the_phone(dp, scratch):
    ws = scratch / "ws"
    write_log(ws / "events" / "workspace.jsonl", [*WS, ev("device.added", device=OTHER)])
    write_log(ws / "tickets" / "T1" / "events.jsonl", [ev("ticket.created", key="K"), asked(), answered(device=OTHER)])
    assert dp.scan_answer(ws, N, PHONE) is None
    assert dp.scan_answer(ws, N, OTHER)["device"] == OTHER


def test_agent_timeout_is_refused(dp, monkeypatch):
    import subprocess

    ctx = type("C", (), {"env": lambda self, extra=None: {}, "orch": lambda self, *a: ["orch", *a], "ws_dir": "/"})()

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("orch", 120)

    monkeypatch.setattr(dp.subprocess, "run", boom)
    with pytest.raises(dp.Refused, match="timed out"):
        dp._agent(ctx, "g", "s", "new", "x")
