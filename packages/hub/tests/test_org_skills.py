"""M3-3: company skills (#1430): the store, the signed ``hub.org_skills`` bundle, the op and the admin routes.

Opus conditions: 1 signed by HK_op, names hub and workspace, monotonic version, a host refuses the wrong workspace, a bad
signature and a lower version; 3 only the link's workspace gets its own skills; 4 text checks (<= 2400 characters, control
and bidi characters stripped, no front matter, a name that cannot shadow orch's own); every act writes an activity record that
names the skill and never its text."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from webapi_helpers import WS, Web

from orch_hub.access import Access, AccessSnapshots
from orch_hub.access.orgskills import org_skills
from orch_hub.access.store import AccessError
from orch_hub.activity.store import ActivityLog
from orch_hub.contract import hub as H
from orch_hub.contract import org_bundles as OB
from orch_hub.contract.canon import cj
from orch_hub.contract.hub import Refused
from orch_hub.keys import HubKeys
from orch_hub.link import Registry
from orch_hub.link.ops import HANDLERS, org_skills_get

ADMIN = "aa" * 16
RLO, ZW = chr(0x202E), chr(0x200B)  # a bidi override and a zero-width space, built so no such character is in this file
WS2 = "b2" * 16
TEXT = "Write invoice numbers as INV-2026-0001. Ask before changing a customer's address."


@pytest.fixture
def env(tmp_path):
    keys = HubKeys.init(tmp_path / "hub")
    acc = Access(str(tmp_path / "access.db"))
    acc.observer = lambda ev: None
    acc.add_user(ADMIN, "root")
    acc.add_member("hub-admin", ADMIN)
    acc.add_user("bb" * 16, "bob")
    t = [1_000_000]

    def clock():
        t[0] += 1000
        return t[0]

    yield keys, acc, AccessSnapshots(acc, keys, clock), org_skills(acc)
    acc.close()


def held(keys):
    return H.verify_card(keys.card(), now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)


def verify(keys, signed, ws=WS, pinned=None):
    return H.verify_org_skills(
        signed, held(keys), workspace_id=ws, dev_mode=True, pinned=pinned, revoked_op_certs=keys.revoked_op_certs()
    )


# --- 4: the text rules -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        ("invoices", TEXT, None),
        ("Invoices", TEXT, "name"),
        ("a", TEXT, "name"),
        ("x" * 41, TEXT, "name"),
        ("orch-tickets", TEXT, "reserved_name"),  # cannot shadow a builtin skill
        ("orchestra", TEXT, "reserved_name"),
        ("invoices", "", "empty"),
        ("invoices", "   \n", "empty"),
        ("invoices", "x" * 2401, "too_long"),
        ("invoices", "x" * 2400, None),
        ("invoices", "---\nname: x\n---\nbody", "front_matter"),
        ("invoices", "  ---\nbody", "front_matter"),
        ("invoices", "ok" + RLO + "txt", "hidden_characters"),  # bidi override
        ("invoices", "ok" + ZW + "hidden", "hidden_characters"),  # zero width
        ("invoices", "ok\x1b[31m", "hidden_characters"),  # a control character
        ("invoices", 5, "empty"),
    ],
)
def test_the_skill_text_rules(name, text, problem):
    assert OB.skill_problem(name, text) == problem


def test_clean_text_strips_control_and_bidi_but_keeps_lines_and_tabs():
    assert OB.clean_text("a" + RLO + "b\x00c" + ZW + "d\r\ne\tf\rg") == "abcd\ne\tf\ng"


def test_put_strips_hidden_characters_and_refuses_what_stays_bad(env):
    _k, _acc, _s, store = env
    store.put("invoices", "keep" + RLO + " this\x00", actor=ADMIN)
    assert store.list()[0]["text"] == "keep this"
    for name, text in (("orch-x", TEXT), ("ok-name", "---\nx"), ("ok-name", "x" * 2401)):
        with pytest.raises(AccessError):
            store.put(name, text, actor=ADMIN)
    assert [s["name"] for s in store.list()] == ["invoices"]


def test_only_a_hub_admin_manages_skills_and_the_list_is_capped(env):
    _k, _acc, _s, store = env
    with pytest.raises(AccessError) as e:
        store.put("invoices", TEXT, actor="bb" * 16)
    assert e.value.code == "forbidden_role"
    for n in range(OB.SKILLS_MAX):
        store.put(f"skill-{n:02d}", TEXT, actor=ADMIN)
    with pytest.raises(AccessError) as e:
        store.put("one-too-many", TEXT, actor=ADMIN)
    assert e.value.code == "too_many"
    store.put("skill-00", TEXT + " More.", actor=ADMIN)  # replacing one is fine at the cap


# --- 1 and 3: the signed bundle ------------------------------------------------------------------------------------------------


def test_a_bundle_carries_only_the_skills_offered_to_that_workspace(env):
    keys, _acc, snaps, store = env
    store.put("invoices", TEXT, actor=ADMIN)
    store.put("support", "Greet customers by name.", actor=ADMIN)
    store.set_offers("invoices", [WS], actor=ADMIN)
    store.set_offers("support", [WS2], actor=ADMIN)
    o1 = verify(keys, snaps.org_skills(WS, "full"))
    o2 = verify(keys, snaps.org_skills(WS2, "full"), ws=WS2)
    assert [s["name"] for s in o1["skills"]] == ["invoices"] and [s["name"] for s in o2["skills"]] == ["support"]
    assert o1["skills"][0]["digest"] == OB.skill_digest("invoices", TEXT)
    assert o1["workspace_id"] == WS and o1["hub_id"] == keys.hub_id and o1["version"] == 1
    assert verify(keys, snaps.org_skills("c3" * 16, "full"), ws="c3" * 16)["skills"] == []  # offered nothing


def test_versions_only_go_up_and_unchanged_content_is_byte_identical(env):
    keys, _acc, snaps, store = env
    store.put("invoices", TEXT, actor=ADMIN)
    store.set_offers("invoices", [WS], actor=ADMIN)
    a = snaps.org_skills(WS, "full")
    assert cj(snaps.org_skills(WS, "full")) == cj(
        a
    )  # unchanged: the same bytes (hosts treat other bytes as a conflict)
    store.put("invoices", TEXT + " Also: add the VAT number.", actor=ADMIN)  # an edit is a new digest and a new version
    b = snaps.org_skills(WS, "full")
    assert verify(keys, b, pinned=verify(keys, a))["version"] == 2
    store.set_offers("invoices", [], actor=ADMIN)  # withdrawing is a new version too (it narrows)
    c = snaps.org_skills(WS, "full")
    assert verify(keys, c, pinned=verify(keys, b))["version"] == 3 and verify(keys, c)["skills"] == []


def test_a_host_refuses_a_wrong_workspace_a_bad_signature_and_a_rollback(env):
    keys, _acc, snaps, store = env
    store.put("invoices", TEXT, actor=ADMIN)
    store.set_offers("invoices", [WS], actor=ADMIN)
    v1 = snaps.org_skills(WS, "full")
    store.put("invoices", TEXT + " Edited.", actor=ADMIN)
    v2 = snaps.org_skills(WS, "full")
    with pytest.raises(Refused) as e:
        verify(keys, v1, ws=WS2)  # the bundle of WS shown to WS2
    assert e.value.code == "other_workspace"
    with pytest.raises(Refused) as e:
        verify(keys, v1, pinned=verify(keys, v2))
    assert e.value.code == "stale_bundle"
    forged = {"o": {**v2["o"], "skills": []}, "sig": v2["sig"]}  # content changed, signature kept
    with pytest.raises(Refused):
        verify(keys, forged)
    other = {"o": {**v2["o"], "kind": "hub.snapshot"}, "sig": v2["sig"]}  # a signature cannot be moved to another kind
    with pytest.raises(Refused):
        verify(keys, other)
    twin = {"o": {**v2["o"], "issued_ms": v2["o"]["issued_ms"] + 1}, "sig": v2["sig"]}
    with pytest.raises(Refused):
        verify(keys, twin, pinned=verify(keys, v2))


def test_the_contract_refuses_a_bundle_whose_text_or_digest_is_bad(env):
    keys, _acc, snaps, store = env
    store.put("invoices", TEXT, actor=ADMIN)
    store.set_offers("invoices", [WS], actor=ADMIN)
    o = verify(keys, snaps.org_skills(WS, "full"))
    good = o["skills"][0]
    for bad in (
        [{**good, "text": "ok" + RLO}],
        [{**good, "digest": "sha256:" + "0" * 64}],
        [{**good, "name": "orch-evil", "digest": OB.skill_digest("orch-evil", TEXT)}],
        [good, good],
        [{**good, "extra": 1}],
    ):
        assert not OB.valid_org_skills(bad)
    assert OB.valid_org_skills([good]) and OB.valid_org_skills([])
    # the signer signs only the kinds it knows, under its own label
    assert "sig_hub_org_skills" in H.LABEL_OF_KIND.values()


def test_a_deleted_skill_leaves_every_bundle_and_unbind_drops_offers(env):
    keys, _acc, snaps, store = env
    store.put("invoices", TEXT, actor=ADMIN)
    store.set_offers("invoices", [WS, WS2], actor=ADMIN)
    snaps.org_skills(WS, "full")
    store.drop_workspace(WS)  # unbind or leave
    assert verify(keys, snaps.org_skills(WS, "full"))["skills"] == []
    assert [s["name"] for s in verify(keys, snaps.org_skills(WS2, "full"), ws=WS2)["skills"]] == ["invoices"]
    store.delete("invoices", actor=ADMIN)
    assert verify(keys, snaps.org_skills(WS2, "full"), ws=WS2)["skills"] == []
    with pytest.raises(AccessError) as e:
        store.delete("invoices", actor=ADMIN)
    assert e.value.code == "unknown_skill"


# --- the op -----------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def reg():
    r = Registry()
    for ws in (WS, WS2):
        r.commit_join(
            ws, ws, "key", "pk", {"name": ws}, {"status": "bound", "binding": {"mode": "full"}}, owner_alias=None
        )
    yield r
    r.close()


def test_the_op_serves_the_link_workspace_only(env, reg):
    keys, _acc, snaps, store = env
    store.put("invoices", TEXT, actor=ADMIN)
    store.set_offers("invoices", [WS], actor=ADMIN)
    c = lambda ws: SimpleNamespace(registry=reg, workspace_id=ws, snapshots=snaps, body={})
    got = org_skills_get(c(WS))
    assert set(got) == {"bundle"} and [s["name"] for s in verify(keys, got["bundle"])["skills"]] == ["invoices"]
    assert verify(keys, org_skills_get(c(WS2))["bundle"], ws=WS2)["skills"] == []
    with pytest.raises(Refused):
        org_skills_get(c("c3" * 16))  # not pinned
    kind, mode, fields = H.LINK_OPS["org_skills_get"]
    assert (kind, mode, fields) == ("w2h", None, {})
    assert "org_skills_get" in HANDLERS


# --- the admin routes ---------------------------------------------------------------------------------------------------------------


def test_the_admin_routes_save_offer_remove_audit_and_are_admin_only(tmp_path):
    w = Web(tmp_path)
    log = w.api.d.activity = ActivityLog(":memory:")
    try:
        admin, ann = w.login(w.admin), w.login(w.ann)
        w.reg.add(WS, "full", name="Product")
        s, o, _ = w.call("PUT", "/admin/skills/invoices", {"text": TEXT + RLO}, token=admin)
        assert s == 200 and o["digest"] == OB.skill_digest("invoices", TEXT)
        s, o, _ = w.call("PUT", "/admin/skills/invoices/offers", {"workspaces": [WS]}, token=admin)
        assert s == 200 and o["offered_to"] == [WS]
        s, o, _ = w.call("GET", "/admin/skills", token=admin)
        assert s == 200 and o["skills"][0]["text"] == TEXT and o["skills"][0]["offered_to"] == [WS]
        assert any(x["workspace_id"] == WS for x in o["workspaces"]) and o["max_chars"] == 2400
        s, o, _ = w.call("PUT", "/admin/skills/orch-evil", {"text": TEXT}, token=admin)
        assert s == 400 and o["error"] == "malformed"
        s, o, _ = w.call("PUT", "/admin/skills/invoices", {"text": 5}, token=admin)
        assert s == 400
        s, o, _ = w.call("PUT", "/admin/skills/invoices/offers", {"workspaces": ["nope"]}, token=admin)
        assert s == 400
        s, o, _ = w.call("PUT", "/admin/skills/ghost/offers", {"workspaces": []}, token=admin)
        assert s == 404 and o["error"] == "unknown_skill"
        for method, path, body in (
            ("GET", "/admin/skills", None),
            ("PUT", "/admin/skills/invoices", {"text": TEXT}),
            ("PUT", "/admin/skills/invoices/offers", {"workspaces": []}),
            ("DELETE", "/admin/skills/invoices", None),
        ):
            s, _o, _ = w.call(method, path, body, token=ann)  # a member who is not a hub-admin
            assert s == 403, path
            s, _o, _ = w.call(method, path, body)  # no session
            assert s == 401, path
        s, o, _ = w.call("DELETE", "/admin/skills/invoices", token=admin)
        assert s == 200
        events = log.list(limit=50)
        assert {"skill.saved", "skill.offered", "skill.removed"} <= {e["type"] for e in events}
        assert all(e["detail"].get("skill") == "invoices" for e in events if e["type"].startswith("skill."))
        assert "INV-2026" not in repr(events)  # the name, never the text
    finally:
        w.close()
