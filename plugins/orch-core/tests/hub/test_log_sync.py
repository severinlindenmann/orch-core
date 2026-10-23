"""C14: the host side of ``log_sync`` (hub contract 10.7): sealed page bytes against the vectors, in-order send, resume,
reset after a restore, epoch rotation, and the gates (full mode, completed join), against the in-process fake hub."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from orch.crypto import suite as S
from orch.crypto.encoding import b64u, unb64u
from orch.hubclient import HubLink, HubRefused, Position, derive_view_key, sync_logs
from orch.hubclient import logsync as LS
from tests.wsproto import r6kit

from . import logview
from .fake import FakeHub
from .test_full_join import WS, Host, mk, sign_wsk  # noqa: F401 - mk is a fixture
from .vec import V, private_key, pub

P4 = V["phase4"]["log_sync"]
WSK = private_key("wsk_a")
TKT = "01J9ZP0000000000000000TKT1"


# --- the vectors ---------------------------------------------------------------------------------------------------


def test_view_key_aad_heads_and_sealed_bytes_match_the_vectors():
    wk = bytes.fromhex(P4["wk_e1_hex"])
    k = derive_view_key(wk, P4["workspace_id"], 1)
    assert k.hex() == P4["view_key_e1_hex"]
    page = P4["page_workspace"]
    lines = P4["workspace_lines"]
    aad = LS.page_aad(P4["workspace_id"], 1, "workspace", 1, 2, page["head"], False)
    assert aad.hex() == P4["aad_hex_workspace"]
    assert LS.event_head(lines[-1]) == P4["heads"]["workspace"] == page["head"]
    assert LS.event_head(P4["ticket_lines"][-1]) == P4["heads"][TKT]
    salt = unb64u(page["sealed"])[:16]  # the vector fixes the salt; the production path draws a fresh one
    built = LS.build_page(
        k,
        P4["workspace_id"],
        1,
        "workspace",
        lines,
        seal=lambda kb, label, aad, pt: S._salted_seal(kb, label, aad, pt, salt),
    )  # the vector fixes the salt: the seam is the test's, production draws a fresh one
    assert built == page  # every field, the sealed text byte for byte


def test_a_production_page_opens_with_the_vector_key_and_a_fresh_salt_each_time():
    k = bytes.fromhex(P4["view_key_e1_hex"])
    a = LS.build_page(k, P4["workspace_id"], 1, "workspace", P4["workspace_lines"])
    b = LS.build_page(k, P4["workspace_id"], 1, "workspace", P4["workspace_lines"])
    assert a["sealed"] != b["sealed"] and {**a, "sealed": ""} == {**b, "sealed": ""}
    assert {**a, "sealed": ""} == {**P4["page_workspace"], "sealed": ""}


def test_the_key_stores_view_key_is_the_contract_derivation(mk):  # noqa: F811
    for e in (1,):
        assert mk.view_key(e) == derive_view_key(mk.wk(e), WS, e)
    from orch.identity.epochs import CustodyRefused

    with pytest.raises(CustodyRefused):
        mk.view_key(7)  # never held
    with pytest.raises(CustodyRefused):
        mk.view_key(0)


def test_the_view_key_is_bound_to_the_workspace_and_the_epoch():
    wk = bytes.fromhex(P4["wk_e1_hex"])
    keys = {derive_view_key(wk, P4["workspace_id"], 1), derive_view_key(wk, P4["workspace_id"], 2),
            derive_view_key(wk, V["ids"]["workspace_b"], 1)}  # fmt: skip
    assert len(keys) == 3 and wk not in keys


# --- a local log the host can read ---------------------------------------------------------------------------------


class Logs:
    """A fake of the host's logs: signed lines with the real chain rules (``prev`` = head of the line before)."""

    def __init__(self) -> None:
        self.lines: dict[str, list[dict[str, Any]]] = {"workspace": []}

    def add(self, log: str, typ: str = "ticket.updated", **extra: Any) -> dict[str, Any]:
        rows = self.lines.setdefault(log, [])
        ev = {
            "v": 2, "id": f"{len(rows) + 1:026d}", "type": typ, "seq": len(rows) + 1, "at": "2026-10-22T20:00:00Z",
            "prev": logview.head_of(rows[-1]) if rows else None, "actor": {"kind": "host"}, "hash_v": 1, **extra,
        }  # fmt: skip
        if log != "workspace":
            ev["ws_seq"] = len(self.lines["workspace"])
        ev["host_sig"] = b64u(S.sign(WSK, logview.host_sig_bytes(WS, log, ev)))
        rows.append(ev)
        return ev

    def restore(self, log: str, keep: int) -> None:
        del self.lines[log][keep:]

    # LogSource
    def logs(self) -> list[str]:
        return ["workspace", *sorted(k for k in self.lines if k != "workspace")]

    def seq(self, log: str) -> int | None:
        return len(self.lines[log]) if log in self.lines else None

    def head_at(self, log: str, seq: int) -> str | None:
        rows = self.lines.get(log, [])
        return logview.head_of(rows[seq - 1]) if 1 <= seq <= len(rows) else None

    def after(self, log: str, after: int, limit: int) -> list[dict[str, Any]]:
        return copy.deepcopy(self.lines[log][after : after + limit])


class Sender:
    def __init__(self, host: Host, logs: Logs) -> None:
        self.host, self.logs = host, logs
        self.pos = Position(host.state.hub_id, host.state.binding_seq)
        self.saved: list[dict[str, Any]] = []

    def run(self, **over: Any) -> LS.SyncReport:
        h = self.host
        link = HubLink(h.wire, hub_id=h.state.hub_id, workspace_id=WS, hk_op_pub=pub("hk_op"), sign=sign_wsk,
                       mode=h.state.mode, now_ms=lambda: V["now_ms"])  # fmt: skip
        kw = dict(state=h.state, workspace_id=WS, call=link.call, source=self.logs, view_key=h.epochs.view_key,
                  position=self.pos, save=lambda p: self.saved.append(p.to_json()))  # fmt: skip
        return sync_logs(**{**kw, **over})

    def pages(self) -> list[dict[str, Any]]:
        return [m["o"]["body"] for m in self.host.wire.sent if m["o"]["op"] == "log_sync"]


@pytest.fixture
def full(mk):  # noqa: F811
    host = Host(FakeHub(), mk)
    host.join_full()
    return host


def hub_view(host: Host) -> dict[str, Any]:
    return host.hub.workspaces[WS].view.summary()


def local_summary(logs: Logs) -> dict[str, Any]:
    return {k: {"seq": len(v), "head": logview.head_of(v[-1])} for k, v in logs.lines.items() if v}


# --- sending ---------------------------------------------------------------------------------------------------------


def test_a_full_join_then_changes_leaves_the_hub_view_at_the_same_head(full):
    logs = Logs()
    for _ in range(3):
        logs.add("workspace", "member.added")
    logs.add(TKT, "ticket.created")
    s = Sender(full, logs)
    r = s.run()
    assert hub_view(full) == local_summary(logs) and r.lines == 4 and r.resets == 0
    logs.add(TKT)
    logs.add("workspace", "settings.changed")
    s.run()
    assert hub_view(full) == local_summary(logs)
    assert full.hub.workspaces[WS].view.logs[TKT][-1][0] == logs.lines[TKT][-1]  # exactly the logged line, host_sig too


def test_lines_go_in_order_workspace_first_then_ticket_logs_in_pages_of_at_most_256(full):
    logs = Logs()
    for _ in range(300):
        logs.add("workspace", "member.added")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    p = s.pages()
    assert [(x["log"], x["from_seq"], x["count"]) for x in p] == [
        ("workspace", 1, 256),
        ("workspace", 257, 44),
        (TKT, 1, 1),
    ]
    assert all(x["epoch"] == 1 and x["reset"] is False for x in p)
    assert hub_view(full) == local_summary(logs)


def test_a_second_pass_with_nothing_new_sends_nothing(full):
    logs = Logs()
    logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    n = len(full.wire.sent)
    assert s.run().pages == 0 and len(full.wire.sent) == n


def test_the_pages_are_sealed_and_the_hub_wire_carries_no_line_text(full):
    logs = Logs()
    logs.add("workspace", "member.added", note="SECRET-TEXT-IN-A-LINE")
    Sender(full, logs).run()
    wire_bytes = b"".join(str(m).encode() for m in full.wire.sent)
    assert b"SECRET-TEXT-IN-A-LINE" not in wire_bytes


def test_resume_after_a_disconnect_sends_only_what_the_hub_has_not_acknowledged(full):
    logs = Logs()
    for _ in range(300):
        logs.add("workspace", "member.added")
    s = Sender(full, logs)
    orig, n = full.wire.send, {"k": 0}

    def flaky(message):
        if message["o"]["op"] == "log_sync":
            n["k"] += 1
            if n["k"] == 2:
                raise ConnectionResetError
        return orig(message)

    full.wire.send = flaky
    with pytest.raises(HubRefused) as e:
        s.run()
    assert e.value.code == "hub_unreachable" and s.pos.logs["workspace"][0] == 256  # the first page was acknowledged
    assert s.saved[-1]["logs"]["workspace"]["seq"] == 256  # and saved before the failure
    full.wire.send = orig
    before = len(s.pages())
    s.run()
    resumed = s.pages()[before:]
    assert [(x["from_seq"], x["count"]) for x in resumed] == [(257, 44)]
    assert hub_view(full) == local_summary(logs)


def test_a_restart_resumes_from_the_saved_position(full):
    logs = Logs()
    for _ in range(5):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    saved = s.saved[-1]
    logs.add("workspace")
    again = Sender(full, logs)
    again.pos = Position.from_json(saved, hub_id=full.state.hub_id, binding_seq=full.state.binding_seq)
    again.run()
    assert [(x["from_seq"], x["count"]) for x in again.pages()[1:]] == [(6, 1)]
    other = Position.from_json(saved, hub_id=full.state.hub_id, binding_seq=full.state.binding_seq + 1)
    assert other.logs == {}  # a position kept for another binding is not trusted
    assert Position.from_json({"junk": 1}, hub_id=full.state.hub_id, binding_seq=1).logs == {}


def test_a_hub_that_lost_its_view_is_resent_from_one_without_a_reset(full):
    logs = Logs()
    for _ in range(4):
        logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    full.hub.workspaces[WS].view = logview.View()  # the hub restored an old database
    logs.add("workspace")
    before = len(s.pages())
    s.run()
    sent = s.pages()[before:]
    assert [(x["log"], x["from_seq"], x["reset"]) for x in sent[:2]] == [
        ("workspace", 5, False),
        ("workspace", 1, False),
    ]
    assert hub_view(full) == local_summary(logs)


# --- the resume probe at a link open (10.7 sender rule 4) -------------------------------------------------------------


def test_the_probe_sends_the_last_acknowledged_line_again_and_changes_nothing(full):
    logs = Logs()
    for _ in range(4):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    before, pos = len(s.pages()), dict(s.pos.logs)
    r = s.run(probe=True)
    sent = s.pages()[before:]
    assert [(x["log"], x["from_seq"], x["count"], x["reset"]) for x in sent] == [("workspace", 4, 1, False)]
    assert r.lines == 0 and s.pos.logs == pos and hub_view(full) == local_summary(logs)


def test_the_probe_goes_first_and_the_new_lines_follow_it(full):
    logs = Logs()
    for _ in range(3):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    logs.add("workspace")
    before = len(s.pages())
    s.run(probe=True)
    assert [(x["from_seq"], x["count"]) for x in s.pages()[before:]] == [(3, 1), (4, 1)]


def test_a_probe_the_hub_answers_with_view_gap_resends_every_log_from_one_without_a_reset(full):
    logs = Logs()
    for _ in range(4):
        logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    full.hub.workspaces[WS].view = logview.View()  # the hub lost its view; the host has nothing new to send
    before = len(s.pages())
    s.run(probe=True)
    sent = s.pages()[before:]
    assert [(x["log"], x["from_seq"], x["reset"]) for x in sent[:2]] == [
        ("workspace", 4, False),
        ("workspace", 1, False),
    ]
    assert (TKT, 1, False) in [(x["log"], x["from_seq"], x["reset"]) for x in sent]
    assert hub_view(full) == local_summary(logs)


def test_a_probe_gap_asks_the_resend_check_for_the_workspace_log(full):
    logs = Logs()
    for _ in range(3):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    full.hub.workspaces[WS].view = logview.View()
    asked: list[str] = []
    s.run(probe=True, allow_resend=lambda log: asked.append(log) or True)
    assert asked == ["workspace"]


def test_a_probe_that_meets_another_chain_is_a_restore_and_is_answered_with_one_reset(full):
    logs = Logs()
    for _ in range(3):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    other = Logs()
    for _ in range(3):
        other.add("workspace", "device.added")
    view = logview.View()
    view.logs["workspace"] = [(e, logview.head_of(e)) for e in other.lines["workspace"]]
    full.hub.workspaces[WS].view = view
    before = len(s.pages())
    s.run(probe=True)
    assert [(x["from_seq"], x["count"], x["reset"]) for x in s.pages()[before:]] == [(3, 1, False), (1, 3, True)]
    assert hub_view(full) == local_summary(logs)


def test_a_probe_with_nothing_acknowledged_sends_nothing_extra(full):
    logs = Logs()
    logs.add("workspace")
    s = Sender(full, logs)
    s.run(probe=True)
    assert [(x["from_seq"], x["count"]) for x in s.pages()] == [(1, 1)]


# --- core#1034: the live check and the per-log resend limit ----------------------------------------------------


def test_core_1034_a_retryable_live_refusal_ends_the_pass_with_its_own_code(full):
    logs = Logs()
    logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)

    def live() -> bool:
        raise HubRefused("epoch_changed")

    with pytest.raises(HubRefused) as e:
        s.run(live=live)
    assert e.value.code == "epoch_changed" and s.pages() == []
    s.run()  # nothing was lost: the next pass sends it
    assert hub_view(full) == local_summary(logs)


def test_core_1034_the_resend_limit_is_asked_per_log(full):
    logs = Logs()
    for _ in range(3):
        logs.add("workspace")
    logs.add(TKT)
    s = Sender(full, logs)
    s.run()
    full.hub.workspaces[WS].view.logs[TKT] = full.hub.workspaces[WS].view.logs[TKT][
        :0
    ]  # the hub lost the ticket log only
    logs.add(TKT)
    asked: list[str] = []
    s.run(allow_resend=lambda log: asked.append(log) or True)
    assert asked == [TKT]


def test_core_1034_the_forced_times_are_kept_per_log_and_an_old_file_is_read_as_empty(full):
    pos = Position(full.state.hub_id, full.state.binding_seq, forced={TKT: [5, 6], "workspace": [7]})
    back = Position.from_json(pos.to_json(), hub_id=pos.hub_id, binding_seq=pos.binding_seq)
    assert back.forced == {TKT: [5, 6], "workspace": [7]}
    old = {"hub_id": pos.hub_id, "binding_seq": pos.binding_seq, "forced": [1, 2, 3], "logs": {}}
    assert Position.from_json(old, hub_id=pos.hub_id, binding_seq=pos.binding_seq).forced == {}


# --- reset after a restore -------------------------------------------------------------------------------------------


def test_a_restore_is_sent_as_a_reset_from_one_and_the_ticket_logs_follow(full):
    logs = Logs()
    for _ in range(2):
        logs.add("workspace")
    logs.add(TKT)
    logs.add(TKT)  # both name ws_seq 2
    for _ in range(2):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    logs.restore("workspace", 2)  # a restore abandons the suffix ...
    logs.add("workspace", "restore", note="a different line")  # ... and the log goes on differently
    before = len(s.pages())
    r = s.run()
    sent = s.pages()[before:]
    assert (sent[0]["log"], sent[0]["from_seq"], sent[0]["reset"]) == ("workspace", 1, True) and r.resets == 1
    assert [(x["log"], x["from_seq"], x["reset"]) for x in sent[1:]] == [(TKT, 1, False)]  # views dropped: resent
    assert hub_view(full) == local_summary(logs)


def test_a_restore_that_only_shortens_the_log_is_also_a_reset(full):
    logs = Logs()
    for _ in range(4):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    logs.restore("workspace", 2)
    before = len(s.pages())
    s.run()
    last = s.pages()[before:]
    assert [(x["from_seq"], x["count"], x["reset"]) for x in last] == [(1, 2, True)]
    assert hub_view(full) == local_summary(logs)


def test_a_fork_the_hub_reports_is_answered_with_one_reset(full):
    logs = Logs()
    for _ in range(3):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    other = Logs()  # a hub holding another chain at the same positions (e.g. an earlier host database)
    for _ in range(3):
        other.add("workspace", "device.added")
    view = logview.View()
    view.logs["workspace"] = [(e, logview.head_of(e)) for e in other.lines["workspace"]]
    full.hub.workspaces[WS].view = view
    s.pos.logs.clear()
    s.pos.logs["workspace"] = (3, logview.head_of(logs.lines["workspace"][-1]))  # position says: all acknowledged
    logs.add("workspace")
    before = len(s.pages())
    s.run()
    assert [(x["from_seq"], x["reset"]) for x in s.pages()[before:]] == [(4, False), (1, True)]
    assert hub_view(full) == local_summary(logs)


# --- epochs ------------------------------------------------------------------------------------------------------


def test_after_a_rotation_pages_use_the_new_epoch_once_it_is_sealed_to_the_hub(full, mk):  # noqa: F811
    logs = Logs()
    logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    r6kit.rotate(mk)  # epoch 2 is committed, the hub has no wrap for it yet
    logs.add("workspace")
    s.run()
    assert s.pages()[-1]["epoch"] == 1  # never under an epoch the hub holds no wrap for
    assert full.seal() == 2  # the host seals WK_2 to the hub (the normal re-seal)
    logs.add("workspace")
    s.run()
    last = s.pages()[-1]
    assert last["epoch"] == 2 and last["from_seq"] == 3
    ws = full.hub.workspaces[WS]
    assert ws.wk == {1: mk.wk(1), 2: mk.wk(2)} and hub_view(full) == local_summary(logs)
    k2 = derive_view_key(mk.wk(2), WS, 2)
    assert LS.build_page(k2, WS, 2, "workspace", logs.lines["workspace"][2:3])["from_seq"] == 3


def test_a_page_under_an_epoch_the_hub_does_not_hold_is_refused_bad_epoch(full, mk):  # noqa: F811
    logs = Logs()
    logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    full.state.sealed = {"epoch": 2, "hxk_pub": full.state.sealed["hxk_pub"]}  # state claims more than the hub took
    r6kit.rotate(mk)
    logs.add("workspace")
    with pytest.raises(HubRefused) as e:
        s.run()
    assert e.value.code == "bad_epoch"


def test_the_view_key_of_an_unknown_epoch_is_refused_and_nothing_is_sent(full):
    logs = Logs()
    logs.add("workspace")
    full.state.sealed = {"epoch": 9, "hxk_pub": full.state.sealed["hxk_pub"]}
    s = Sender(full, logs)
    n = len(full.wire.sent)
    with pytest.raises(HubRefused) as e:
        s.run()
    assert e.value.code == "no_epoch" and len(full.wire.sent) == n


# --- gates -----------------------------------------------------------------------------------------------------------


def test_selective_mode_never_sends(mk):  # noqa: F811
    host = Host(FakeHub(), mk)
    host.join_selective()
    logs = Logs()
    logs.add("workspace")
    s = Sender(host, logs)
    n = len(host.wire.sent)
    with pytest.raises(HubRefused) as e:
        s.run(view_key=lambda epoch: b"\0" * 32)
    assert e.value.code == "wrong_mode" and len(host.wire.sent) == n and s.pages() == []
    link = HubLink(host.wire, hub_id=host.state.hub_id, workspace_id=WS, hk_op_pub=pub("hk_op"), sign=sign_wsk,
                   mode="selective", now_ms=lambda: V["now_ms"])  # fmt: skip
    with pytest.raises(HubRefused) as e2:  # and the link itself refuses the op before it is signed
        link.call("log_sync", {})
    assert e2.value.code == "wrong_mode" and len(host.wire.sent) == n


def test_nothing_is_sent_before_the_join_is_complete(mk):  # noqa: F811
    host = Host(FakeHub(), mk)
    host.wire.refuse_ops = {"wk_seal"}
    with pytest.raises(HubRefused):
        host.join_full()  # bound at the hub, but the seal did not get through
    assert host.state.status == "bound" and host.state.sealed is None
    logs = Logs()
    logs.add("workspace")
    s = Sender(host, logs)
    n = len(host.wire.sent)
    with pytest.raises(HubRefused) as e:
        s.run()
    assert e.value.code == "not_joined" and len(host.wire.sent) == n
    host.state.status = "joining"
    with pytest.raises(HubRefused) as e:
        s.run()
    assert e.value.code == "not_joined"
    with pytest.raises(HubRefused) as e:
        s.run(state=None)
    assert e.value.code == "not_joined" and len(host.wire.sent) == n


def test_after_a_leave_nothing_is_sent(full, mk):  # noqa: F811
    logs = Logs()
    logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    full.leave()
    logs.add("workspace")
    n = len(full.wire.sent)
    with pytest.raises(HubRefused) as e:
        s.run()
    assert e.value.code == "not_joined" and len(full.wire.sent) == n


def test_a_position_for_another_binding_is_refused(full):
    logs = Logs()
    logs.add("workspace")
    s = Sender(full, logs)
    s.pos = Position(full.state.hub_id, full.state.binding_seq + 1)
    with pytest.raises(HubRefused) as e:
        s.run()
    assert e.value.code == "not_joined"


def test_an_unreadable_log_is_skipped_not_guessed(full):
    logs = Logs()
    logs.add("workspace")
    logs.add(TKT)
    logs.seq = lambda log: None if log == TKT else len(logs.lines[log])  # type: ignore[method-assign]
    s = Sender(full, logs)
    s.run()
    assert [x["log"] for x in s.pages()] == ["workspace"]


def test_a_line_bigger_than_a_page_stops_that_log_without_failing_the_pass(full):
    """Rule 3: never skip the line, send nothing more of the log, report it; the pass does not fail or retry forever."""
    logs = Logs()
    logs.add("workspace", "member.added")
    logs.add("workspace", "handoff.written", blob="x" * (LS.MAX_LINE + 10))
    logs.add("workspace", "member.added")
    logs.add(TKT, "ticket.created")
    s = Sender(full, logs)
    r = s.run()
    assert r.stuck == {"workspace": 2} and s.pos.stuck == {"workspace": 2}
    assert [(x["log"], x["from_seq"], x["count"]) for x in s.pages()] == [
        ("workspace", 1, 1)
    ]  # line 3 never skipped to
    assert s.pos.logs["workspace"][0] == 1 and TKT not in s.pos.logs
    assert not LS.pending(logs, s.pos)  # nothing to retry while the line stays broken
    assert Position.from_json(s.pos.to_json(), hub_id=s.pos.hub_id, binding_seq=s.pos.binding_seq).stuck == {
        "workspace": 2
    }
    n = len(s.pages())
    s.run()
    assert len(s.pages()) == n  # a second pass sends nothing more


def test_a_stuck_log_is_worked_out_again_once_the_log_changes(full):
    logs = Logs()
    logs.add("workspace", "member.added")
    logs.add("workspace", "handoff.written", blob="x" * (LS.MAX_LINE + 10))
    s = Sender(full, logs)
    s.run()
    logs.restore("workspace", keep=1)  # a restore takes the broken line out
    logs.add("workspace", "member.added")
    assert LS.pending(logs, s.pos)
    r = s.run()
    assert r.stuck == {} and hub_view(full) == local_summary(logs)


def test_a_ticket_log_with_a_big_line_does_not_stop_the_workspace_log(full):
    logs = Logs()
    logs.add("workspace", "member.added")
    logs.add(TKT, "ticket.created", blob="x" * (LS.MAX_LINE + 10))
    s = Sender(full, logs)
    r = s.run()
    assert r.stuck == {TKT: 1} and hub_view(full) == {"workspace": local_summary(logs)["workspace"]}


def test_page_size_shrinks_to_fit(full):
    logs = Logs()
    for _ in range(4):
        logs.add("workspace", "handoff.written", blob="y" * 300_000)
    s = Sender(full, logs)
    s.run()
    p = s.pages()
    assert all(len(x["sealed"]) <= LS.MAX_SEALED for x in p) and sum(x["count"] for x in p) == 4 and len(p) > 1
    assert hub_view(full) == local_summary(logs)


# --- l2-c14 adversarial (Opus security review, 22 Oct) ------------------------------------------------------------


def test_l2_c14_1_a_leave_during_a_pass_stops_the_next_page(full):
    """L2-C14-1 (P1): a leave (or switch/unbind) that lands while a pass is on the network must stop the pass before
    its next page. A hostile hub that ignores the leave and keeps acknowledging must not receive lines committed after
    the leave, sealed under the old epoch it still holds (the 10.7 claim "a host that rotated the hub out cannot leak
    later content")."""
    logs = Logs()
    logs.add("workspace", "member.added")
    logs.add(TKT, "ticket.created")
    s = Sender(full, logs)
    link = HubLink(full.wire, hub_id=full.state.hub_id, workspace_id=WS, hk_op_pub=pub("hk_op"), sign=sign_wsk,
                   mode=full.state.mode, now_ms=lambda: V["now_ms"])  # fmt: skip
    after_leave: list[dict[str, Any]] = []
    left = []

    def call(op: str, body: dict[str, Any]) -> dict[str, Any]:
        if left:  # the hostile hub: takes everything, ignores the leave
            after_leave.append(body)
            return {"seq": body["from_seq"] + body["count"] - 1, "head": body["head"]}
        reply = link.call(op, body)
        full.leave()  # the owner leaves while the pass is on the network
        left.append(True)
        logs.add(TKT, "ticket.updated", note="WRITTEN-AFTER-THE-LEAVE")
        return reply

    try:
        s.run(call=call)
    except HubRefused:
        pass  # stopping with a refusal is fine
    assert after_leave == [], f"{len(after_leave)} page(s) sent to the hub after the leave"


def test_l2_c14_1_a_line_committed_during_a_pass_waits_for_the_next_pass(full):
    """The pass sends nothing beyond the head each log had when it began (the next pass carries the rest)."""
    logs = Logs()
    logs.add("workspace", "member.added")
    logs.add(TKT, "ticket.created")
    s = Sender(full, logs)
    link = HubLink(full.wire, hub_id=full.state.hub_id, workspace_id=WS, hk_op_pub=pub("hk_op"), sign=sign_wsk,
                   mode=full.state.mode, now_ms=lambda: V["now_ms"])  # fmt: skip
    seen: list[dict[str, Any]] = []

    def call(op: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append(body)
        reply = link.call(op, body)
        if len(seen) == 1:
            logs.add("workspace", "member.added")
            logs.add(TKT, "ticket.updated")
        return reply

    r = s.run(call=call)
    assert [(x["log"], x["from_seq"], x["count"]) for x in seen] == [("workspace", 1, 1), (TKT, 1, 1)] and r.lines == 2
    s.run()
    assert hub_view(full) == local_summary(logs)


def test_l2_c14_1_the_live_check_ends_the_pass_before_the_next_page(full):
    logs = Logs()
    logs.add("workspace", "member.added")
    logs.add(TKT, "ticket.created")
    s = Sender(full, logs)
    n = {"calls": 0}

    def live() -> bool:
        n["calls"] += 1
        return n["calls"] < 2  # yes for the first page, no for the second

    with pytest.raises(HubRefused) as e:
        s.run(live=live)
    assert e.value.code == "not_joined" and len(s.pages()) == 1


def test_l2_c14_3_forced_resends_are_rate_limited(full):
    logs = Logs()
    for _ in range(3):
        logs.add("workspace")
    s = Sender(full, logs)
    s.run()
    full.hub.workspaces[WS].view = logview.View()  # the hub "lost" everything: view_gap on the next page
    logs.add("workspace")
    before = len(s.pages())
    with pytest.raises(HubRefused) as e:
        s.run(allow_resend=lambda log: False)
    assert e.value.code == "resend_limited" and len(s.pages()) == before + 1  # only the page that was refused


def test_l2_c14_2_the_view_key_is_only_for_the_epoch_sealed_to_the_hub():
    from orch.host.hubops import _Epochs

    class Store:  # holds every epoch: the gate, not the store, must refuse
        asked: list[int] = []

        def view_key(self, epoch: int) -> bytes:
            self.asked.append(epoch)
            return b"k" * 32

    st, sealed = Store(), {"v": 1}
    e = _Epochs(st, None, lambda: sealed["v"])
    assert e.view_key(1) == b"k" * 32
    with pytest.raises(HubRefused) as x:
        e.view_key(2)  # held, but newer than the epoch sealed to the hub
    assert x.value.code == "no_epoch" and st.asked == [1]
    sealed["v"] = None
    with pytest.raises(HubRefused):
        e.view_key(1)
    with pytest.raises(HubRefused):
        _Epochs(st).view_key(1)  # no provider: fail closed
