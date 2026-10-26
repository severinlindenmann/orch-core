"""L2 (phase-4 contract pass, V3-D23) adversarial tests: orch-hub PR #22 at 5c04a8e. Each test asserts the behaviour the
review asks for and was ``xfail(strict=True)`` while the finding was open (state/l2-review-p4.md, L2P4-n); the follow-up contract commit
fixed them, so they now pass as plain tests."""

from __future__ import annotations

import pytest

from orch_hub.contract import hub as H
from orch_hub.contract import phase4 as P4
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.contract import vectorgen_p4 as GP

TICKET = G.TICKET


def _created(title: str) -> dict:
    return {"type": "ticket.created", "ticket_type": "feature", "title": title}


# --- L2P4-3: the text rule lets through private-use, unassigned and default-ignorable "blank" code points -------------


# L2P4-3: Co (private use) passes the text rule; U+F8FF renders as a glyph on iOS (fixed)
def test_l2p4_3_text_rule_refuses_private_use():
    with pytest.raises(H.Refused) as r:
        P4.check_action(_created("Approve " + chr(0xF8FF) + " payroll"), role="edit")
    assert r.value.code == "malformed"


# L2P4-3: Cn (unassigned) passes; a newer Unicode may make it Cf (interop) (fixed)
def test_l2p4_3_text_rule_refuses_unassigned():
    with pytest.raises(H.Refused) as r:
        P4.check_action(_created("Fix ͸ login"), role="edit")
    assert r.value.code == "malformed"


# L2P4-3: U+3164 HANGUL FILLER (Lo, Default_Ignorable) makes a blank title (fixed)
def test_l2p4_3_text_rule_refuses_blank_default_ignorable_title():
    for blank in ("ㅤ", "ᅟ", "ﾠ", "a͏", "x️"):
        with pytest.raises(H.Refused):
            P4.check_action(_created(blank), role="edit")


# L2P4-3: a lone surrogate raises UnicodeEncodeError, not Refused('malformed') (fixed)
def test_l2p4_3_text_rule_lone_surrogate_is_a_refusal_not_a_crash():
    with pytest.raises(H.Refused) as r:
        P4.check_action(_created("a\ud800b"), role="edit")
    assert r.value.code == "malformed"


# --- L2P4-6: channel registration does not see the permanent channel revocation list ------------------------------------


# L2P4-6: verify_channel_register takes no revoked_channels; §6.2 says permanent (fixed)
def test_l2p4_6_revoked_channel_key_cannot_register_again():
    reg = GP.register()
    revoked = [H.channel_id(G.K.pub["channel_alice_phone"])]  # the key register() signs with
    with pytest.raises(H.Refused) as r:
        P4.verify_channel_register(
            reg, hub_id=G.HUB_ID, challenge=GP.CHALLENGE, now_ms=G.NOW, dev_mode=True, revoked_channels=revoked
        )
    assert r.value.code == "channel_revoked"


# --- L2P4-9: a workspace-log reset leaves ticket lines that run ahead of the new workspace view -------------------------


# L2P4-9: reset of the workspace log does not re-check held ticket ws_seq (fixed)
def test_l2p4_9_workspace_reset_does_not_keep_ticket_lines_ahead_of_it():
    wk = {1: GP.WK[1]}
    kw = {"workspace_id": G.WS_A, "wsk_pub": S.public_bytes(G.K.priv["wsk_a"]), "wk_epochs": wk}
    views: dict = {}
    for body in (GP.page("workspace", GP.WS_LINES), GP.page(TICKET, GP.TK_LINES)):
        views = P4.apply_view_page(views, body, **kw)
    assert len(views[TICKET]) == 2  # ticket seq 2 has ws_seq 2
    views = P4.apply_view_page(views, GP.page("workspace", GP.WS_LINES[:1], reset=True), **kw)
    assert len(views["workspace"]) == 1
    # the ticket view still holds a line with ws_seq 2 > the workspace view: it must be dropped or truncated
    assert len(views.get(TICKET, [])) <= 1


# --- L2P4-1, L2P4-2, D7 (ported from the review's vector names) -------------------------------------------------------


def test_l2p4_1_selective_floor_comes_from_the_member_role():
    assert H.MEMBER_ROLE_FLOOR == {"owner": "admin", "maintainer": "edit", "member": "edit", "viewer": "read"}


def test_l2p4_2_check_built_never_skips_a_missing_hash():
    action = {"type": "verdict.given", "log": TICKET, "outcome": "pass", "hash": "sha256:" + "aa" * 32}
    with pytest.raises(H.Refused) as r:
        P4.check_built(action, TICKET, {"type": "verdict.given"})
    assert r.value.code == "malformed"


def test_l2p4_d7a_pairing_code_alone_is_not_enough():
    with pytest.raises(H.Refused) as r:
        P4.verify_channel_register(
            GP.register(), hub_id=G.HUB_ID, challenge={**GP.CHALLENGE, "session": False}, now_ms=G.NOW, dev_mode=True
        )
    assert r.value.code == "no_session"
