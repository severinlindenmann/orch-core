from orch_dashboard import adapters


def test_adapter_maps_real_data_only(stub):
    row = stub.call("ticket.query", {})["data"]["rows"][0]
    row["status"] = "in_progress"
    row["lines"] = ["ignored"]
    row["claim"] = {"session": "s_1", "agent": "claude-code"}
    result = adapters.summary(row)
    assert result["status"] == "in-progress"
    assert result["head_seq"] == row["head_seq"]
    assert "lines" not in result
    # What the row does not carry is unknown (None), never zero or false: a host without read_ext/1 sends none of it.
    for key in ("tasks", "criteria", "restricted", "blocking_questions"):
        row.pop(key, None)
    result = adapters.summary(row)
    assert result["progress"] is None and result["restricted"] is None and result["blocking_questions"] is None
    assert result["claim"] == {"agent": "claude-code", "for": "", "session": "s_1", "live": None}
    assert result["turn"]["who"] == "agent:claude-code"


def test_needs_kinds_map_to_the_ui(stub):
    kinds = ["question", "approve", "no_eligible", "acknowledge", "addon"]
    data = {
        "items": [
            {"kind": k, "key": "DEMO-0001", "uid": "u", "ref": "plan", "detail": "x", "who": ["p"]} for k in kinds
        ]
        + [{"kind": "approve", "key": "DEMO-0001", "uid": "u", "ref": "verify", "detail": "", "who": ["p"]}],
        "sub_cursor": "c",
    }
    result = adapters.today(
        data,
        [{"uid": "u", "title": "T", "status": "open", "needs": ["x"]}],
        stub.ws,
        "2026-10-11T00:00:00Z",
        "p",
        "owner",
    )
    assert [n["kind"] for n in result["needs_you"]] == [
        "question",
        "approval",
        "no_eligible",
        "acknowledge",
        "addon",
        "verdict",
    ]
    assert all(n["title"] == "T" and n["since"] == "" for n in result["needs_you"])  # no `since`: unknown, ''
    assert result["needs_you"][2]["text"] == "No one can approve the plan gate"
    assert result["counts"] == {"open": 1} and result["waiting_on_others"]["count"] == 0
    viewer = adapters.today(data, [], stub.ws, "2026-10-11T00:00:00Z", "p", "viewer")
    assert viewer["needs_you"] == [] and len(viewer["read_only_open"]) == 6


def test_changes_pair_files_with_their_hunks():
    unified = (
        "diff --git a/a.txt b/a.txt\nindex 1..2 100644\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n one\n+two\n"
        "diff --git a/b.bin b/b.bin\nBinary files differ\n"
    )
    data = {
        "sources": [{"repo": "code", "branch": "feat/x", "base_sha": "b" * 40, "head_sha": "h" * 40}],
        "files": [
            {"repo": "code", "path": "a.txt", "old_path": None, "status": "M", "add": 1, "del": 0, "binary": False},
            {"repo": "code", "path": "b.bin", "old_path": None, "status": "A", "add": 0, "del": 0, "binary": True},
        ],
        "unified": unified,
        "truncated": False,
    }
    result = adapters.changes(data, "DEMO-0001")
    assert result["head"] == "h" * 40 and result["branch"] == "feat/x" and result["additions"] == 1
    assert result["files"][0]["lines"] == "@@ -1 +1,2 @@\n one\n+two"
    assert result["files"][1]["lines"].startswith("(binary")
    assert result["sources"][0]["repo"] == "code"


def test_an_approvals_auth_is_shown_as_it_is_never_relabelled_as_a_password():
    """H5c: `convenience` (no presence) must never read as "password"; an auth value this adapter doesn't know keeps
    its own name, and an unreadable one is "unknown"."""
    from orch_dashboard.adapters.tickets import _approval

    def presence(auth):
        return _approval({"person": "p_ana", "at": "2026-10-12T10:00:00Z", "auth": auth})["presence"]

    assert presence("passphrase") == "password"
    assert presence("secure-enclave") == "touchid"
    assert presence("convenience") == "convenience"
    assert presence("hardware-key") == "hardware-key"
    assert presence("Pass‮word") == "unknown"
    assert presence({"not": "a string"}) == "unknown"


def test_a_task_receipt_keeps_its_attested_by_label():
    from orch_dashboard.adapters.tickets import _task

    base = {"id": "T1", "text": "t", "state": "done", "proves": []}
    receipt = {"exit": 0, "ms": 5, "commit": None, "at": "2026-10-12T10:00:00Z"}
    labelled = _task(base, {"receipt": {**receipt, "attested_by": "agent"}, "leased_by": None}, True)
    assert labelled["receipt"] == {"exit": 0, "ms": 5, "attested_by": "agent"}
    # A receipt the host did not label says "unknown"; it is never shown as verified.
    assert _task(base, {"receipt": receipt}, True)["receipt"] == {"exit": 0, "ms": 5, "attested_by": "unknown"}


def test_a_tasks_verify_command_reaches_the_ui_as_text():
    from orch_dashboard.adapters.tickets import _task, _verify

    base = {"id": "T1", "text": "t", "state": "todo", "proves": []}
    assert _task({**base, "verify": "make test"}, None, True)["verify"] == {"cmd": "make test"}
    assert _task(base, None, True)["verify"] is None
    assert _verify({"cmd": "x"}) == {"cmd": "x"} and _verify("") is None and _verify(7) is None


def test_workspace_badge_equals_todays_count_and_leaves_notices_out(stub):
    """RH-6: the workspace number sums only the kinds Today counts (question, approval, verdict)."""
    kinds = ["question", "approve", "no_eligible", "acknowledge", "addon"]
    items = [{"kind": k, "key": "DEMO-0001", "uid": "u", "ref": "plan", "detail": "x", "who": ["p"]} for k in kinds]
    items.append({"kind": "approve", "key": "DEMO-0001", "uid": "u", "ref": "verify", "detail": "", "who": ["p"]})
    today = adapters.today({"items": items, "sub_cursor": "c"}, [], stub.ws, "2026-10-11T00:00:00Z", "p", "owner")
    counted = [n for n in today["needs_you"] if n["kind"] in ("question", "approval", "verdict")]
    assert adapters.actionable_count(items, "owner") == len(counted) == 3
    assert adapters.actionable_count(items, "viewer") == 0  # a viewer's Today has no "needs you"


def test_host_info_passes_only_the_closed_dev_shape():
    from orch_dashboard.adapters import host_info

    wid = "a" * 32
    assert host_info({"workspaces": []}) == {}
    assert host_info(None) == {}
    assert host_info({"dev": {"sim_convenience": False}}) == {}
    got = host_info(
        {
            "dev": {"sim_convenience": True, "sandbox": "0123abcd", "extra": "x"},
            "alarms": [
                {"code": "sandbox_workspace_registered", "workspace_id": wid, "text": "<b>x</b>"},
                {"code": "sandbox_workspace_registered", "workspace_id": "../../etc"},
                {"code": "dev_convenience_refused", "count": 3, "first": "x", "text": "<i>"},
                {"code": "Not A Code!"},
                {"code": "something_new"},
                "junk",
            ],
        }
    )
    assert got == {
        "dev": {"sim_convenience": True, "sandbox": "0123abcd"},
        "alarms": [
            {"code": "sandbox_workspace_registered", "workspace_id": wid},
            {"code": "sandbox_workspace_registered"},
            {"code": "dev_convenience_refused"},
            {"code": "something_new"},
        ],
    }
    assert "sandbox" not in host_info({"dev": {"sim_convenience": True, "sandbox": "NOT-HEX"}})["dev"]


def test_a_gate_carries_what_an_approval_still_lacks():
    """B11: core's `missing` reaches the page, which never offers Approve while it lacks anything."""
    from orch_dashboard.adapters.tickets import _gate

    assert _gate({"state": "open", "missing": ["plan"]}, True)["missing"] == ["plan"]
    assert _gate({"state": "open", "missing": []}, True)["missing"] == []
    assert "missing" not in _gate({"state": "open"}, True)
