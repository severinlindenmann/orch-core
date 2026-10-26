"""Core H12d-2 on the dashboard server: `reader.list` (cap `reader/1`) through an allow-list, the reader fields of
`member.list`, and `Error.detail` of the two click-to-sign refusal codes, closed per code before it reaches the page."""

import json

import pytest

from orch_dashboard import adapters
from orch_dashboard.errors import HostError, closed_detail, host_response
from orch_dashboard.hostlink import CAPS, OPS

DEV_A = "d_" + "a" * 32
DEV_B = "d_" + "b" * 32
BIDI = "‮"


def body(exc: HostError) -> dict:
    return json.loads(host_response(exc).body)


# ---- Error.detail: closed per code ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,reason",
    [
        ("sign.convenience_unavailable", r)
        for r in ("disabled", "relay_paired", "other_member", "phone_device", "reader_version", "pair_device")
    ]
    + [
        ("sign.convenience_readers", r)
        for r in ("no_declaration", "reader_too_old", "declaration_unknown", "relay_too_old", "unknown_readers")
    ],
)
def test_every_reason_of_the_two_codes_reaches_the_page_with_its_devices(code, reason):
    got = body(
        HostError(
            {"code": code, "message": "m", "retryable": False, "detail": {"reason": reason, "devices": [DEV_A]}}, 3
        )
    )
    assert got["error"]["detail"] == {"reason": reason, "devices": [DEV_A]}


@pytest.mark.parametrize(
    "code,reason",
    [
        ("sign.convenience_unavailable", "no_declaration"),  # a reason of the other code
        ("sign.convenience_readers", "disabled"),
        ("sign.convenience_readers", "relay_reader_old"),  # H12d-3, not in this slice
        ("sign.convenience_unavailable", "Disabled"),
        ("sign.convenience_unavailable", "disabled" + BIDI),
        ("sign.convenience_unavailable", 1),
        ("sign.convenience_unavailable", None),
    ],
)
def test_an_unknown_reason_is_dropped_never_echoed(code, reason):
    got = body(HostError({"code": code, "message": "m", "retryable": False, "detail": {"reason": reason}}, 3))
    assert "detail" not in got["error"]
    assert closed_detail(code, {"reason": reason, "devices": [DEV_A]}) == {"devices": [DEV_A]}


def test_extra_detail_keys_are_dropped():
    detail = {
        "reason": "reader_too_old",
        "devices": [DEV_A],
        "required": 1,
        "declared": 0,
        "path": "/Users/x",
        "fix": "y",
    }
    got = body(HostError({"code": "sign.convenience_readers", "message": "m", "retryable": False, "detail": detail}, 3))
    assert got["error"]["detail"] == {"reason": "reader_too_old", "devices": [DEV_A]}


@pytest.mark.parametrize(
    "devices",
    [
        "d_" + "a" * 32,  # not a list
        [{"id": DEV_A}],  # an object, not an id
        [DEV_A, "d_" + "A" * 32],  # upper-case hex
        [DEV_A, "d_" + "a" * 31],  # short
        [DEV_A, "p_" + "a" * 32],  # a person
        [DEV_A, DEV_A + BIDI],
        [DEV_A, "Mara's phone"],  # a label
        [DEV_A, 5],
        [f"d_{i:032x}" for i in range(65)],  # more than 64
    ],
)
def test_devices_must_be_a_list_of_device_ids_or_it_is_dropped_whole(devices):
    assert closed_detail("sign.convenience_unavailable", {"reason": "phone_device", "devices": devices}) == {
        "reason": "phone_device"
    }


def test_devices_alone_and_duplicates():
    assert closed_detail("sign.convenience_unavailable", {"devices": [DEV_A, DEV_B, DEV_A]}) == {
        "devices": [DEV_A, DEV_B]
    }
    assert closed_detail("sign.convenience_unavailable", {"devices": []}) == {"devices": []}
    assert closed_detail("sign.convenience_unavailable", {}) is None
    assert closed_detail("sign.convenience_unavailable", "disabled") is None
    assert closed_detail("sign.convenience_unavailable", None) is None


@pytest.mark.parametrize(
    "code,detail",
    [("term.not_attachable", {"state": s}) for s in ("starting", "running", "exited", "lost")]
    + [("run.limit", {"which": w}) for w in ("agent_ws", "agent_person", "shell_person", "runner")]
    + [("launch.no_grant", {"reason": r}) for r in ("none", "expiring", "scope", "revoked")]
    + [("runner.disabled", {"missing": m}) for m in ("addon", "machine", "terminals")]
    + [("ticket.key_not_next", {"next_key": "DEMO-0042"})],
)
def test_each_earlier_code_keeps_its_closed_detail(code, detail):
    got = body(HostError({"code": code, "message": "m", "retryable": False, "detail": {**detail, "path": "/x"}}, 3))
    assert got["error"]["detail"] == detail


@pytest.mark.parametrize(
    "code,detail",
    [
        ("term.not_attachable", {"state": "gone"}),
        ("run.limit", {"which": "everyone"}),
        ("launch.no_grant", {"reason": "disabled"}),  # a reason of another code
        ("runner.disabled", {"missing": "machine" + BIDI}),
        ("runner.disabled", {"reason": "machine"}),  # a key of another code
        ("ticket.key_not_next", {"next_key": "demo-42"}),
        ("ticket.key_not_next", {"next_key": 42}),
        ("reader.decl_invalid", {"reason": "signature"}),
        ("human.request_state", {"reason": "disabled", "devices": [DEV_A]}),
        ("not_found", {"state": "running"}),
    ],
)
def test_unknown_codes_keys_and_values_carry_no_detail(code, detail):
    got = body(HostError({"code": code, "message": "m", "retryable": False, "detail": detail}, 3))
    assert "detail" not in got["error"] and got["error"]["code"] == code


def test_the_rest_of_the_error_is_unchanged_and_fix_rules_still_apply():
    err = {
        "code": "sign.convenience_readers",
        "message": "no_declaration: the new reader has no declaration",
        "retryable": False,
        "hint": "h",
        "fix": {"argv": ["orch", "host", "convenience", "off", "--workspace", "DEMO"]},
        "detail": {"reason": "no_declaration", "devices": [DEV_A]},
    }
    got = body(HostError(err, 3))
    assert got == {
        "ok": False,
        "exit": 3,
        "error": {
            "code": "sign.convenience_readers",
            "message": err["message"],
            "retryable": False,
            "hint": "h",
            "fix": err["fix"],
            "detail": {"reason": "no_declaration", "devices": [DEV_A]},
        },
    }


# ---- the host link and the adapter ----------------------------------------------------------------------------------
def test_reader_list_is_a_read_of_its_cap_and_reader_declare_is_never_sent():
    assert OPS["reader.list"] == 1 and "reader/1" in CAPS
    assert "reader.declare" not in OPS


ROW = {
    "device": DEV_A,
    "person": "p_" + "a" * 32,
    "dk_hash": "demo-not-a-real-dk-hash",
    "reader": 1,
    "level": 1,
    "digest": "sha256:" + "0" * 64,
    "decl": {"v": 1, "secret": "demo-not-a-real-decl"},
}


def test_the_adapter_forwards_only_its_allow_list():
    got = adapters.settings.readers(
        {"readers": [ROW, {**ROW, "device": DEV_B, "reader": 0, "level": 0}], "required_reader": 1}
    )
    dumped = got.model_dump(mode="json")
    assert dumped == {
        "available": True,
        "readers": [
            {"device": DEV_A, "person": ROW["person"], "reader": 1, "level": 1},
            {"device": DEV_B, "person": ROW["person"], "reader": 0, "level": 0},
        ],
        "required_reader": 1,
    }
    assert (
        "dk_hash" not in json.dumps(dumped) and "digest" not in json.dumps(dumped) and "decl" not in json.dumps(dumped)
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"device": "d_x"},
        {"device": DEV_A + BIDI},
        {"person": "Mara"},
        {"level": None},
        {"level": "1"},
        {"level": -1},
        {"reader": True},
        {"level": 1.0},
    ],
)
def test_a_bad_reader_row_is_internal_not_forwarded(patch):
    with pytest.raises(HostError) as caught:
        adapters.settings.readers({"readers": [{**ROW, **patch}], "required_reader": 0})
    assert caught.value.error["code"] == "internal"


@pytest.mark.parametrize("required", [None, "1", -1, True])
def test_a_bad_required_reader_is_internal(required):
    with pytest.raises(HostError):
        adapters.settings.readers({"readers": [], "required_reader": required})


def test_member_list_device_fields_are_checked_one_by_one():
    base = {"id": DEV_A, "auth": None, "scopes": ["look"], "valid": True}

    def roster(d):
        members = [{"person": "p_a", "name": "N", "role": "owner", "devices": [{**base, **d}]}]
        return adapters.settings.roster({"members": members})[0].devices[0]

    got = roster(
        {
            "label": "Work phone\nsecond",
            "paired_at": "2026-10-12T09:00:00Z",
            "relay": True,
            "cert_expires_at": None,
            "reader": 1,
        }
    )
    assert (got.label, got.label_hidden, got.paired_at, got.relay, got.cert_expires_at, got.reader) == (
        "Work phone\nsecond",
        False,
        "2026-10-12T09:00:00Z",
        True,
        None,
        1,
    )
    hidden = roster({"label": "Mara" + BIDI + "s phone"})
    assert hidden.label is None and hidden.label_hidden is True
    assert roster({"label": "x" * 81}).label is None
    assert roster({"label": ""}).label is None
    odd = roster({"paired_at": "yesterday", "relay": "yes", "reader": "1"})
    assert (odd.paired_at, odd.relay, odd.reader) == (None, None, None)
    absent = roster({})  # a host before read_ext/3: unknown, not zero
    assert (absent.label, absent.relay, absent.reader) == (None, None, None)


# ---- the route ------------------------------------------------------------------------------------------------------
def test_the_readers_route_answers_levels_and_required_reader(worlds, p1):
    devices = list(p1.store.state.workspace.devices)
    world = worlds(p1.owner.ref, reader_levels={devices[0]: 1}, required_reader=1)
    res = world.get(f"/api/w/{world.stub.ws}/readers")
    assert res.status_code == 200, res.text
    got = res.json()
    assert got["available"] is True and got["required_reader"] == 1
    assert {r["device"]: r["level"] for r in got["readers"]}[devices[0]] == 1
    assert all(set(r) == {"device", "person", "reader", "level"} for r in got["readers"])
    session = world.get("/api/session").json()
    assert "readers" in session["workspaces"][world.stub.ws]
    members = world.get(f"/api/w/{world.stub.ws}/members").json()
    assert next(d for m in members for d in m["devices"] if d["id"] == devices[0])["reader"] == 1


def test_a_host_without_reader_1_answers_not_available(worlds, p1):
    world = worlds(p1.owner.ref, readers_cap=False)
    got = world.get(f"/api/w/{world.stub.ws}/readers").json()
    assert got == {"available": False, "readers": [], "required_reader": None}
    assert "readers" not in world.get("/api/session").json()["workspaces"][world.stub.ws]


def test_the_readers_route_needs_a_session(client, stub):
    assert client.get(f"/api/w/{stub.ws}/readers").status_code == 401


def test_the_detail_table_matches_cores_per_code_table():
    """errors.DETAIL_KEYS mirrors core's orch.host.errors.DETAIL_KEYS: a code, key or value core adds or removes must be
    a deliberate change here too (Opus delta L1). The dashboard is at least as strict: `next_key` is checked against
    the ticket-key format where core leaves it open (None), and an over-long device list is dropped, never cut."""
    from orch.host import errors as core

    from orch_dashboard import errors as dash

    assert dash.DETAIL_KEYS.keys() == core.DETAIL_KEYS.keys()
    for code, keys in core.DETAIL_KEYS.items():
        assert dash.DETAIL_KEYS[code].keys() == keys.keys(), code
        for key, allowed in keys.items():
            mine = dash.DETAIL_KEYS[code][key]
            if isinstance(allowed, tuple):
                assert set(mine) == set(allowed), (code, key)
            elif allowed is None:
                assert mine == dash.KEY, (code, key)  # stricter: a ticket key, not free text
            else:
                assert mine == allowed, (code, key)
    assert dash.DEVICES_MAX == core.DEVICES_MAX
    assert dash.DEVICE.pattern == core.DEVICE_ID.pattern
    assert dash.TICKET_KEY.pattern == core._KEY.pattern
