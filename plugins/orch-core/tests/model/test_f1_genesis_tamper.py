"""The genesis and tampered-chain vectors through ``orch.model.replay`` with the real verifier (ticket-format §5.5,
§5.11): a genesis that fails any of the six checks creates no workspace, a pinned genesis or workspace id that
differs is refused, and a mutated log line breaks the chain at the stated ``seq`` (ticket log and workspace log)."""

import pytest

from orch import canon
from orch.identity import CryptoVerifier
from orch.model import admit, replay

from .f1_runner import load

G = load("genesis.json")
W = G["workspace_id"]
NOW = "2026-10-10T11:00:00Z"


def rp(events, pin=None, ws=W, tickets=None):
    return replay(
        events, tickets or {}, verifier=CryptoVerifier(), now=NOW, expected_workspace_id=ws, expected_genesis=pin
    )


def test_a_valid_genesis_creates_the_workspace_and_is_the_trust_root():
    e = G["valid"]["event"]
    st = rp([e])
    assert st.workspace.genesis == G["genesis"] == canon.event_head(e)
    assert st.workspace.workspace_id == W and list(st.workspace.members.values())[0].role == "owner"
    assert st.workspace.roster_v == 1 and not st.chain_errors and not st.workspace.invalid
    assert rp([e], pin=G["genesis"]).workspace.genesis == G["genesis"]


def test_a_pinned_genesis_or_workspace_id_that_differs_is_refused():
    e = G["valid"]["event"]
    for st in (rp([e], pin="sha256:" + "00" * 32), rp([e], ws="f" * 32)):
        assert st.workspace.genesis is None and not st.workspace.members
        assert st.chain_errors and st.chain_errors[0].seq == 1


@pytest.mark.parametrize("c", G["refused"], ids=lambda c: c["name"])
def test_a_genesis_that_fails_a_check_creates_no_workspace(c):
    st = rp([c["event"]])
    assert st.workspace.genesis is None and not st.workspace.members
    if c["replay"] == "chain.broken":
        assert [e.code for e in st.chain_errors] == ["chain.broken"] and not st.workspace.invalid
    else:
        assert [i.code for i in st.workspace.invalid] == [c["replay"]] and not st.chain_errors
    assert getattr(admit(rp([]), c["event"], log="workspace"), "code", None).value == c["append"]


T = load("tamper.json")


def _replay_with(case):
    ws = list(T["workspace_events"])
    tl = {u: list(v) for u, v in T["ticket_events"].items()}
    if case["log"] == "workspace":
        ws = case["events"]
    else:
        tl[case["log"]] = case["events"]
    return rp(ws, pin=T["genesis"], tickets=tl)


def test_the_untouched_logs_replay_clean_and_the_lines_are_cj():
    st = rp(T["workspace_events"], pin=T["genesis"], tickets=T["ticket_events"])
    assert not st.chain_errors and not st.workspace.invalid
    assert canon.check_chain(T["workspace_events"]) == T["heads"]["workspace"]
    for uid, evs in T["ticket_events"].items():
        assert canon.check_chain(evs) == T["heads"][uid]
    for hex_line, e in zip(T["workspace_lines_hex"], T["workspace_events"], strict=True):
        assert canon.parse_event_line(bytes.fromhex(hex_line)) == e and canon.event_line(e) == bytes.fromhex(hex_line)
    for hex_line, e in zip(T["ticket_lines_hex"], T["ticket_events"][next(iter(T["ticket_events"]))], strict=True):
        assert canon.parse_event_line(bytes.fromhex(hex_line)) == e


# orch-only: where ``canon.check_chain`` (prev/seq links only, no signatures) stops. The vector states the one F1
# outcome (``chain_broken_at``); this layer split is orch's and is kept out of the shared file.
CHECK_CHAIN_STOPS_AT = {
    "payload_changed_not_resigned": 3,
    "payload_changed_host_resigned": 3,
    "event_deleted": 2,
    "events_swapped": 3,
    "at_goes_back_host_resigned": None,  # check_chain sees links only, not `at`
    "ws_seq_goes_back_host_resigned": None,  # valid links; replay must check per-log workspace positions
    "bad_person_sig_and_ws_seq": None,  # signatures and ws_seq are outside check_chain
    "prev_rewritten": 3,
    "seq_rewritten": 3,
    "host_sig_swapped": 3,
    "tail_truncated": None,
    "genesis_payload_changed": 2,
    "member_role_raised_not_resigned": 4,
    "member_role_raised_host_resigned": 4,
    "workspace_event_deleted": 2,
    "host_field_set_valid": None,
    "host_field_set_sig": None,
    "host_field_set_auth": None,
    "host_field_set_roster_v": None,
    "agent_field_set_valid": None,
    "agent_field_set_sig": None,
}


@pytest.mark.parametrize("c", T["cases"], ids=lambda c: c["name"])
def test_check_chain_stops_at_the_first_broken_link(c):
    at = CHECK_CHAIN_STOPS_AT[c["name"]]
    if at is None:
        canon.check_chain(c["events"])
    else:
        with pytest.raises(canon.ChainError) as ei:
            canon.check_chain(c["events"])
        assert ei.value.seq == at, c["mutation"]


@pytest.mark.parametrize("c", T["cases"], ids=lambda c: c["name"])
def test_a_tampered_line_breaks_the_chain_where_the_vector_says(c):
    st = _replay_with(c)
    broken = [x.seq for x in st.chain_errors if x.log == c["log"]]
    assert broken == ([c["chain_broken_at"]] if c["chain_broken_at"] else []), c["mutation"]
    if "replay_expect" in c:
        assert [x.code for x in st.chain_errors if x.log == c["log"]] == [c["replay_expect"]], c["mutation"]
    if c.get("replay_only"):
        prefix = {**c, "events": c["events"][: c["chain_broken_at"] - 1]}
        before = _replay_with(prefix)
        assert not before.chain_errors
        event = c["events"][c["chain_broken_at"] - 1]
        assert admit(before, event, log=c["log"]).code == "chain.bad_ws_seq"
    stopped = c.get("ticket_events_stopped_from", {})
    for uid in T["ticket_events"]:
        if uid == c["log"]:
            continue  # the directly tampered log was checked above
        got = [x.seq for x in st.chain_errors if x.log == uid]
        assert got == ([stopped[uid]] if uid in stopped else []), (uid, c["mutation"])
        if stopped.get(uid) == 1:
            assert uid not in st.tickets
    assert not st.workspace.invalid, c["mutation"]
    assert not [i for log in st._core.logs.values() for i in log.invalid if i.freezes], c["mutation"]
