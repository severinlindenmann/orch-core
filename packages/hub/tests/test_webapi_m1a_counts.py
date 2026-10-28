"""The company home and rail are scoped to the signed-in person's roles."""

from webapi_helpers import ORIGIN, WS, WS_OTHER, Web

from orch_hub.switch import SwitchService


def test_home_and_rail_only_count_my_workspaces(tmp_path):
    w = Web(tmp_path)
    try:
        bob = w.env.user("bob")
        w.env.acc.add_group("bob-team")
        w.env.acc.add_member("bob-team", bob)
        w.env.acc.grant(WS_OTHER, "bob-team", "edit")
        w.reg.add(WS_OTHER, "full", name="Bob's workspace")
        rows = {
            WS: [{"item_id": "ann-item", "title": "Ann's question"}],
            WS_OTHER: [{"item_id": "bob-1", "title": "Bob's question"}, {"item_id": "bob-2"}],
        }
        reads = []

        def needs(workspace, user):
            reads.append((workspace, user))
            return rows[workspace]

        w.view.needs_you = needs
        w.api.d.switch = SwitchService(
            hub_origin=lambda: ORIGIN,
            hub_name="Company",
            badge=lambda workspace, user: len(needs(workspace, user)),
        )
        for user, workspace, count in [(w.ann, WS, 1), (bob, WS_OTHER, 2)]:
            token = w.login(user)
            reads.clear()
            status, rail, _ = w.call("GET", "/rail", token=token)
            assert status == 200
            assert [r["workspace_id"] for r in rail["workspaces"]] == [workspace]
            assert sum(r["badge"] for r in rail["workspaces"]) == count
            assert rail["workspaces"][0]["last_seen"] is None
            status, home, _ = w.call("GET", "/me/needs-you", token=token)
            assert status == 200
            assert len(home["items"]) == count
            assert {r["workspace_id"] for r in home["items"]} == {workspace}
            assert all(pair == (workspace, user) for pair in reads)
            status, spaces, _ = w.call("GET", "/me/workspaces", token=token)
            assert status == 200
            assert [r["workspace_id"] for r in spaces["workspaces"]] == [workspace]
    finally:
        w.close()


def test_last_seen_read_and_offline_are_scoped(tmp_path):
    w = Web(tmp_path)
    try:
        w.api.d.switch = SwitchService(hub_origin=lambda: ORIGIN)
        original = w.reg.binding_state
        w.reg.binding_state = lambda ws: {**original(ws), "status": "unbound", "last_seen": 1700000000}
        token = w.login(w.ann)
        for path in ("/rail", "/me/workspaces"):
            status, body, _ = w.call("GET", path, token=token)
            assert status == 200
            assert body["workspaces"][0]["last_seen"] == 1700000000
            assert body["workspaces"][0]["online"] is False
        status, body, _ = w.call("GET", "/me/needs-you", token=token)
        assert status == 200
        assert body["offline"] == [WS]
        assert body["items"] == []
    finally:
        w.close()
