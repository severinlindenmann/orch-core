"""Shares (Publish add-on, P6): the host link's ops, the typed routes and their closed bodies, the projection (host
text is display-only), the two signed actions, and the link rule: the one answer that holds a link is `send`'s, and
the token is nowhere else (not in a log, a header, a later read, an error)."""

import logging

import pytest
from conftest import ORIGIN
from pydantic import ValidationError

from orch_dashboard import hostlink
from orch_dashboard import signing as handoff
from orch_dashboard.errors import HostError, allowed_error
from orch_dashboard.shares import listing, sent, share_row

IDEM = "a_01M4Y46B485AXD4CBEJZ15Z1TV"
SID = "0123456789abcdef0123456789abcdef"
STAGED = "ps_0123456789abcdef0123456789abcdef"
TOKEN = "Zk3-token_SECRET0123456789abcdefghijklmnopqrs"  # 43 URL-safe characters, like the host's
LINK = f"http://127.0.0.1:8104/s/{SID}#t={TOKEN}"
SHARE = {
    "id": SID,
    "ticket": "DEMO-1",
    "kind": "once",
    "state": "live",
    "expires_at": "2026-10-25T08:00:00Z",
    "view_limit": 3,
    "views": 1,
    "files": ["report.md"],
    "created_at": "2026-10-18T08:00:00Z",
    "server": "http://127.0.0.1:8103",
}
FILE = {"name": "report.md", "size": 120, "sha256": "ab" * 32}
LIST = {
    "configured": True,
    "server": "http://127.0.0.1:8103",
    "share": "http://127.0.0.1:8104",
    "workspace": "ab" * 16,
    "owner": "p_owner",
    "live": True,
    "shares": [SHARE],
    "staged": [
        {
            "staged_id": STAGED,
            "ticket": "DEMO-1",
            "kind": "secret",
            "days": 7,
            "views": 0,
            "origin": "agent",
            "files": [FILE],
            "created_at": "2026-10-18T08:00:00Z",
        }
    ],
}
PREPARED = {
    "staged_id": STAGED,
    "ticket": "DEMO-1",
    "kind": "secret",
    "days": 7,
    "views": 0,
    "origin": "person",
    "files": [FILE],
    "server": "http://127.0.0.1:8103",
    "share": "http://127.0.0.1:8104",
    "needs": "a person's signature",
    "words": "Publish approval: share files outside this computer\nTicket: DEMO-1",
}
SENT = {
    "share_id": SID,
    "ticket": "DEMO-1",
    "kind": "secret",
    "link": LINK,
    "expires_at": "2026-10-25T08:00:00Z",
    "view_limit": 0,
    "shown_once": True,
}


def hdr(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN, "Content-Type": "application/json"}


class Host:
    welcome = {"peer": {"person": "p_me"}, "ops": {}}

    def __init__(self):
        self.calls = []
        self.answers = {}
        self.cap = True

    def supports(self, op, cap):
        return self.cap and op.startswith("publish.") and cap == "publish/1"

    def call(self, op, args=None, write=False, idem=None, **kw):
        self.calls.append((op, args, write, idem))
        answer = self.answers[op]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def host(gateway, monkeypatch):
    fake = Host()
    fake.answers["ticket.query"] = {"rows": [{"key": "DEMO-1", "title": "Fix billing"}], "next_page": None}
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: fake.call(op, args))
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def url(stub, path):
    return f"/api/w/{stub.ws}{path}"


def test_the_publish_ops_are_ours_under_cap_publish_1():
    assert "publish/1" in hostlink.CAPS
    for op in (
        "publish.list",
        "publish.stage_upload",
        "publish.prepare",
        "publish.send",
        "publish.revoke",
        "publish.discard",
        "publish.register",
    ):
        assert hostlink.OPS[op] == 1 and op in hostlink.PUBLISH_OPS
    # `publish.share` is the inner op of a signed hand-off only: never a plain op of this process
    assert "publish.share" not in hostlink.OPS


def test_list_asks_for_the_live_state_and_projects_plain_fields_without_any_link(client, token, stub, host):
    host.answers["publish.list"] = {**LIST, "link": LINK, "token": TOKEN, "extra": "x"}
    res = client.get(url(stub, "/shares"), headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls[0] == ("publish.list", {"live": True}, False, None)
    got = res.json()
    assert got["available"] is True and got["configured"] is True and got["live"] is True
    assert got["server"] == "http://127.0.0.1:8103" and got["share"] == "http://127.0.0.1:8104"
    assert got["shares"][0] | {"title": None} == {
        "id": SID,
        "ticket": "DEMO-1",
        "title": None,
        "kind": "once",
        "state": "live",
        "expires_at": "2026-10-25T08:00:00Z",
        "view_limit": 3,
        "views": 1,
        "files": ["report.md"],
        "created_at": "2026-10-18T08:00:00Z",
        "server": "http://127.0.0.1:8103",
    }
    assert got["shares"][0]["title"] == "Fix billing"
    assert got["staged"][0]["files"] == [FILE] and got["staged"][0]["origin"] == "agent"
    assert TOKEN not in res.text and "link" not in res.text and "extra" not in res.text
    assert res.headers["cache-control"] == "no-store"


def test_a_host_without_publish_says_unavailable(client, token, stub, host):
    host.cap = False
    assert client.get(url(stub, "/shares"), headers=hdr(token)).json() == {"available": False}
    assert host.calls == []


def test_list_for_one_ticket_names_the_ticket(client, token, stub, host):
    host.answers["publish.list"] = LIST
    assert client.get(url(stub, "/shares?ticket=DEMO-1"), headers=hdr(token)).status_code == 200
    assert host.calls[0][1] == {"live": True, "ref": "DEMO-1"}
    assert client.get(url(stub, "/shares?ticket=../x"), headers=hdr(token)).status_code == 400
    assert client.get(url(stub, "/shares?x=1"), headers=hdr(token)).status_code == 400


def test_projection_withholds_hidden_text_and_unknown_values():
    got = share_row(
        {**SHARE, "kind": "weird", "state": "gone", "files": ["a‮b.md", "ok.md"], "views": -1, "server": "x​y"}
    )
    assert got["kind"] == "unknown" and got["state"] == "unknown" and got["views"] is None
    assert got["files"] == ["ok.md"] and got["server"] is None
    assert share_row({**SHARE, "id": "../x"}) is None
    out = listing({**LIST, "server": "ftp://evil", "share": "https://ok.example", "workspace": "nope", "owner": "q"})
    assert (
        out["server"] is None
        and out["share"] == "https://ok.example"
        and out["workspace"] is None
        and out["owner"] is None
    )
    assert listing(None)["shares"] == [] and listing("junk")["configured"] is False


def test_prepare_is_a_write_with_the_browsers_idem_and_nothing_leaves(client, token, stub, host):
    host.answers["publish.prepare"] = PREPARED
    body = {"ticket": "DEMO-1", "artifacts": ["report.md"], "kind": "once", "days": 3, "views": 3, "idem": IDEM}
    res = client.post(url(stub, "/shares/prepare"), json=body, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls[-1] == (
        "publish.prepare",
        {"ref": "DEMO-1", "artifacts": ["report.md"], "kind": "once", "days": 3, "views": 3},
        True,
        IDEM,
    )
    got = res.json()
    assert got["staged"]["staged_id"] == STAGED and got["staged"]["files"] == [FILE]
    assert got["server"] == "http://127.0.0.1:8103" and "words" not in res.text and "needs" not in res.text


UPLOADED = {"upload_id": "pu_0123456789abcdef0123456789abcdef", "name": "notes.txt", "size": 5, "sha256": "cd" * 32}


def test_upload_hands_one_file_to_the_host_and_answers_only_plain_fields(client, token, stub, host):
    host.answers["publish.stage_upload"] = {**UPLOADED, "extra": "x", "path": "/secret"}
    body = {"name": "notes.txt", "data": "aGVsbG8=", "idem": IDEM}
    res = client.post(url(stub, "/shares/upload"), json=body, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls[-1] == ("publish.stage_upload", {"name": "notes.txt", "b64": "aGVsbG8="}, True, IDEM)
    assert res.json() == UPLOADED and "secret" not in res.text


def test_an_upload_answer_without_an_id_is_an_error(client, token, stub, host):
    host.answers["publish.stage_upload"] = {"name": "notes.txt"}
    body = {"name": "notes.txt", "data": "aGVsbG8=", "idem": IDEM}
    assert client.post(url(stub, "/shares/upload"), json=body, headers=hdr(token)).status_code >= 500


def test_prepare_names_picked_files_and_artifacts_together(client, token, stub, host):
    host.answers["publish.prepare"] = PREPARED
    up = UPLOADED["upload_id"]
    body = {"ticket": "DEMO-1", "artifacts": ["report.md"], "uploads": [up], "idem": IDEM}
    res = client.post(url(stub, "/shares/prepare"), json=body, headers=hdr(token))
    assert res.status_code == 200, res.text
    args = host.calls[-1][1]
    assert args["artifacts"] == ["report.md"] and args["uploads"] == [up]
    only = {"ticket": "DEMO-1", "uploads": [up], "idem": IDEM}
    assert client.post(url(stub, "/shares/prepare"), json=only, headers=hdr(token)).status_code == 200
    assert "artifacts" not in host.calls[-1][1] and host.calls[-1][1]["uploads"] == [up]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "n.txt", "data": "aGVsbG8="},  # no idem
        {"name": "../n.txt", "data": "aGVsbG8=", "idem": IDEM},
        {"name": ".hidden", "data": "aGVsbG8=", "idem": IDEM},
        {"name": "n.txt", "data": "not base64 !!", "idem": IDEM},
        {"name": "n.txt", "data": "", "idem": IDEM},
        {"name": "n.txt", "data": "A" * (1_398_104 + 4), "idem": IDEM},  # over the cap: never reaches the host
        {"name": "n.txt", "data": "aGVsbG8=", "idem": IDEM, "path": "/etc/passwd"},
        {"name": "n.txt", "data": "aGVsbG8=", "idem": IDEM, "ticket": "DEMO-1"},
        {"name": "n.txt", "data": ["aGVsbG8="], "idem": IDEM},
        {"names": ["a.txt", "b.txt"], "data": "aGVsbG8=", "idem": IDEM},  # one file per call
    ],
)
def test_upload_bodies_are_closed(client, token, stub, host, body):
    assert client.post(url(stub, "/shares/upload"), json=body, headers=hdr(token)).status_code == 400
    assert host.calls == []


def test_a_body_far_over_the_cap_is_refused_before_it_is_read(client, token, stub, host):
    body = {"name": "n.txt", "data": "A" * 3_000_000, "idem": IDEM}
    assert client.post(url(stub, "/shares/upload"), json=body, headers=hdr(token)).status_code == 413
    assert host.calls == []


def test_upload_needs_a_session_and_the_right_origin(client, stub, host):
    body = {"name": "n.txt", "data": "aGVsbG8=", "idem": IDEM}
    assert client.post(url(stub, "/shares/upload"), json=body).status_code in (401, 403)
    assert host.calls == []


@pytest.mark.parametrize(
    "path,body",
    [
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"]}),
        ("prepare", {"ticket": "DEMO-1", "uploads": ["pu_xyz"], "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "uploads": ["pu_" + "0" * 32] * 2, "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "uploads": ["pu_" + "0" * 32], "artifacts": ["x.md"] * 20, "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": [], "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["../x"], "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": [".hidden"], "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md", "A.md"], "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"], "days": 60, "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"], "days": "7", "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"], "kind": "public", "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"], "kind": "once", "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"], "kind": "secret", "views": 1, "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"], "kind": "once", "views": 5, "idem": IDEM}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["a.md"], "idem": IDEM, "path": "/etc/passwd"}),
        ("prepare", {"ticket": "DEMO-1", "artifacts": ["x.md"] * 21, "idem": IDEM}),
        ("send", {"staged_id": STAGED}),
        ("send", {"staged_id": "ps_xyz", "idem": IDEM}),
        ("send", {"staged_id": STAGED, "idem": IDEM, "link": "x"}),
        ("revoke", {"share_id": "../x", "idem": IDEM}),
        ("revoke", {"share_id": SID, "idem": IDEM, "force": True}),
        ("discard", {"staged_id": SID, "idem": IDEM}),
        ("register", {}),
        ("register", {"idem": IDEM, "server": "http://evil"}),
    ],
)
def test_bodies_are_closed(client, token, stub, host, path, body):
    assert client.post(url(stub, f"/shares/{path}"), json=body, headers=hdr(token)).status_code == 400
    assert host.calls == []


def test_send_answers_the_link_once_and_the_token_is_nowhere_else(client, token, stub, host, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    host.answers["publish.send"] = SENT
    res = client.post(url(stub, "/shares/send"), json={"staged_id": STAGED, "idem": IDEM}, headers=hdr(token))
    assert res.status_code == 200, res.text
    assert host.calls[-1] == ("publish.send", {"staged_id": STAGED}, True, IDEM)
    got = res.json()
    assert got["link"] == LINK and got["share_id"] == SID and got["kind"] == "secret"
    assert "shown_once" not in got
    assert res.headers["cache-control"] == "no-store"
    # nothing else that this process says or keeps holds the token
    host.answers["publish.list"] = {**LIST, "shares": [SHARE]}
    later = client.get(url(stub, "/shares"), headers=hdr(token))
    assert TOKEN not in later.text and TOKEN not in str(later.headers)
    out, err = capfd.readouterr()
    assert TOKEN not in out and TOKEN not in err
    assert TOKEN not in caplog.text and SID + "#" not in caplog.text
    assert TOKEN not in repr(client.app.state.__dict__)
    # a replayed read of the same share never carries it
    assert TOKEN not in client.get(url(stub, "/shares?ticket=DEMO-1"), headers=hdr(token)).text


def test_send_without_a_link_is_an_error_never_an_empty_success(client, token, stub, host):
    for answer in (
        {**SENT, "link": None},
        {**SENT, "link": "short"},
        {k: v for k, v in SENT.items() if k != "link"},
        "junk",
    ):
        host.answers["publish.send"] = answer
        res = client.post(url(stub, "/shares/send"), json={"staged_id": STAGED, "idem": IDEM}, headers=hdr(token))
        assert res.status_code >= 400 and res.json()["ok"] is False
    with pytest.raises(HostError):
        sent({**SENT, "link": "x" * 2000})


def test_a_refused_send_has_no_link_and_the_closed_detail_only(client, token, stub, host):
    host.answers["publish.send"] = HostError(
        {
            "code": "publish.server_refused",
            "message": "the server does not know this workspace yet: its admin must allow it",
            "retryable": False,
            "detail": {"reason": "unknown_workspace", "body": LINK, "extra": TOKEN},
        },
        3,
    )
    res = client.post(url(stub, "/shares/send"), json={"staged_id": STAGED, "idem": IDEM}, headers=hdr(token))
    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "publish.server_refused" and err["detail"] == {"reason": "unknown_workspace"}
    assert TOKEN not in res.text
    assert (
        allowed_error({"code": "publish.not_approved", "message": "x", "detail": {"reason": LINK}}).get("detail")
        is None
    )


def test_not_approved_and_changed_are_ordinary_conflicts(client, token, stub, host):
    host.answers["publish.send"] = HostError({"code": "publish.not_approved", "message": "a person must sign first"}, 3)
    res = client.post(url(stub, "/shares/send"), json={"staged_id": STAGED, "idem": IDEM}, headers=hdr(token))
    assert res.status_code == 409 and res.json()["error"]["code"] == "publish.not_approved"


def test_revoke_discard_register_are_writes_with_the_browsers_idem(client, token, stub, host):
    host.answers["publish.revoke"] = {"share_id": SID, "state": "revoked"}
    host.answers["publish.discard"] = {"discarded": True}
    host.answers["publish.register"] = {"registered": True, "server": "http://127.0.0.1:8103", "result": "unchanged"}
    assert client.post(
        url(stub, "/shares/revoke"), json={"share_id": SID, "idem": IDEM}, headers=hdr(token)
    ).json() == {"revoked": True}
    assert host.calls[-1] == ("publish.revoke", {"share_id": SID}, True, IDEM)
    assert client.post(
        url(stub, "/shares/discard"), json={"staged_id": STAGED, "idem": IDEM}, headers=hdr(token)
    ).json() == {"discarded": True}
    assert host.calls[-1] == ("publish.discard", {"staged_id": STAGED}, True, IDEM)
    assert client.post(url(stub, "/shares/register"), json={"idem": IDEM}, headers=hdr(token)).json() == {
        "registered": True,
        "result": "unchanged",
    }
    assert host.calls[-1] == ("publish.register", {}, True, IDEM)


@pytest.mark.parametrize(
    "method,path",
    [("get", "/shares"), ("post", "/shares/prepare"), ("post", "/shares/send"), ("post", "/shares/revoke")],
)
def test_no_session_no_foreign_origin_no_foreign_site(client, token, stub, host, method, path):
    body = {"json": {"staged_id": STAGED, "idem": IDEM}} if method == "post" else {}
    call = getattr(client, method)
    assert (
        call(url(stub, path), headers={"Origin": ORIGIN, "Content-Type": "application/json"}, **body).status_code == 401
    )
    bad = {**hdr(token), "Origin": "http://evil.example"}
    assert call(url(stub, path), headers=bad, **body).status_code in (403, 421)
    assert call(url(stub, path), headers={**hdr(token), "Sec-Fetch-Site": "cross-site"}, **body).status_code == 403
    assert host.calls == []
    if method == "post":
        wrong_type = {**hdr(token), "Content-Type": "text/plain"}
        assert call(url(stub, path), headers=wrong_type, content="x").status_code == 400


# -- the two signed actions --------------------------------------------------------------------------------------------


def test_setup_is_a_settings_change_naming_both_addresses():
    action = handoff.PublishSetup(action="publish_setup", server="http://127.0.0.1:8103", share="http://127.0.0.1:8104")
    op, args = handoff.host_call(action)
    assert op == "settings.set" and op in handoff.WORKSPACE_OPS
    assert args == {"set": {"publish": {"server": "http://127.0.0.1:8103", "share": "http://127.0.0.1:8104"}}}
    check = handoff.expected(action)
    assert check([{"type": "settings.changed", "set": args["set"]}]) is True
    assert (
        check(
            [
                {
                    "type": "settings.changed",
                    "set": {"publish": {"server": "http://127.0.0.1:8103", "share": "https://x.example"}},
                }
            ]
        )
        is False
    )
    assert check([{"type": "settings.changed", "set": {**args["set"], "grant_hours": 168}}]) is False
    assert check([]) is False


@pytest.mark.parametrize(
    "server,share",
    [
        ("http://evil.example", "https://ok.example"),  # http is for this computer only
        ("https://Upper.example", "https://ok.example"),
        ("https://ok.example/path", "https://other.example"),
        ("ftp://ok.example", "https://other.example"),
        ("https://ok.example", ""),
        ("https://user@ok.example", "https://other.example"),
        ("http://127.0.0.1:0", "https://other.example"),
    ],
)
def test_setup_refuses_addresses_core_would_refuse(server, share):
    with pytest.raises(ValidationError):
        handoff.PublishSetup(action="publish_setup", server=server, share=share)


def test_setup_accepts_one_address_for_both():
    one = handoff.PublishSetup(action="publish_setup", server="https://ok.example", share="https://ok.example")
    assert one.server == one.share


def test_setup_accepts_https_and_loopback():
    handoff.PublishSetup(
        action="publish_setup", server="https://pub.example.org", share="https://apps.example.org:8443"
    )
    handoff.PublishSetup(action="publish_setup", server="http://localhost:8103", share="http://127.0.0.1:8104")


def test_share_signs_exactly_the_hosts_words_for_this_staged_copy():
    action = handoff.PublishShare(action="publish_share", key="DEMO-1", staged_id=STAGED)
    assert handoff.host_call(action) == ("publish.share", {"staged_id": STAGED})
    check = handoff.expected(action)
    lines = [
        handoff.PUBLISH_MARK,
        "Ticket: DEMO-1",
        "Files (1, 120 bytes):",
        "- report.md  (120 bytes, sha256 abababababababab)",
        f"Staged copy: {STAGED}",
        f"Manifest: sha256:{'ab' * 32}",
    ]
    text = "\n".join(lines)
    assert check([{"type": "log.added", "text": text}]) is True
    other = text.replace(STAGED, "ps_" + "f" * 32)
    assert check([{"type": "log.added", "text": other}]) is False  # another staged copy
    assert check([{"type": "log.added", "text": "hello\n" + text}]) is False  # does not start with the mark
    assert check([{"type": "ticket.updated", "text": text}]) is False
    assert check([{"type": "log.added", "text": text}, {"type": "log.added", "text": text}]) is False
    assert check([{"type": "log.added"}]) is False


def test_the_approval_mark_is_the_cores():
    from orch.publishclient.approval import MARK

    assert handoff.PUBLISH_MARK == MARK


@pytest.mark.parametrize("staged", ["ps_xyz", "ps_" + "A" * 32, SID, "../x"])
def test_share_needs_a_real_staged_id(staged):
    with pytest.raises(ValidationError):
        handoff.PublishShare(action="publish_share", key="DEMO-1", staged_id=staged)


def test_share_is_a_click_op_in_core():
    from orch.click.protocol import op_class

    assert op_class("publish.share") == "click"


# -- the feature: Publish is listed only while the add-on is ON -------------------------------------------------------


class AddonLink:
    def __init__(self, rows):
        self.rows = rows

    def call(self, op, args=None):
        assert op == "addon.list"
        if isinstance(self.rows, Exception):
            raise self.rows
        return {"addons": self.rows}


def test_publish_is_a_feature_only_while_the_addon_is_active():
    from orch_dashboard.gateway import Gateway

    on = AddonLink([{"name": "dashboard", "state": "active"}, {"name": "publish", "state": "active", "enabled": True}])
    assert Gateway.addon_on(on, "publish") is True
    for state in ("not_granted", "disabled", "changed", "missing", "purged"):
        assert Gateway.addon_on(AddonLink([{"name": "publish", "state": state}]), "publish") is False
    assert Gateway.addon_on(AddonLink([{"name": "worktrees", "state": "active"}]), "publish") is False
    assert Gateway.addon_on(AddonLink(HostError({"code": "host.unavailable"}, 3)), "publish") is False
    assert Gateway.addon_on(AddonLink([None, "x"]), "publish") is False


def test_the_session_lists_publish_only_when_on_and_served(gateway, stub, monkeypatch):
    class Link(Host):
        def has(self, cap):
            return False

        def call(self, op, args=None, **kw):
            return {"addons": self.rows}

        def supports(self, op, cap):
            return self.cap and (op.startswith("publish.") and cap == "publish/1")

    link = Link()
    link.rows = [{"name": "publish", "state": "active"}]
    monkeypatch.setattr(gateway, "enabled", lambda ws: link)
    assert "publish" in gateway.features(stub.ws)
    link.rows = [{"name": "publish", "state": "not_granted"}]
    assert "publish" not in gateway.features(stub.ws)
    link.rows = [{"name": "publish", "state": "active"}]
    link.cap = False
    assert "publish" not in gateway.features(stub.ws)


def test_both_publish_actions_still_prepare_through_the_terminal_hand_off(gateway, stub, monkeypatch):
    """The hand-off route plans them as `human.prepare` inner ops (`publish.share` needs the host to serve send)."""

    class Link(Host):
        welcome = {"peer": {"person": "p_me"}, "ops": {"settings.set": 1, "publish.send": 1}}

    link = Link()
    monkeypatch.setattr(gateway, "link", lambda ws: link)
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: {"doc": {"labels": []}, "gates": {}})
    setup = handoff.plan(
        gateway,
        stub.ws,
        handoff.PublishSetup(action="publish_setup", server="http://127.0.0.1:8103", share="http://127.0.0.1:8104"),
    )
    assert setup.op == "settings.set" and setup.args["set"]["publish"]["server"] == "http://127.0.0.1:8103"
    share = handoff.plan(gateway, stub.ws, handoff.PublishShare(action="publish_share", key="DEMO-1", staged_id=STAGED))
    assert share.op == "publish.share" and share.args == {"staged_id": STAGED}
    link.welcome = {"peer": {"person": "p_me"}, "ops": {"settings.set": 1}}  # a host that does not serve Publish
    with pytest.raises(HostError):
        handoff.plan(gateway, stub.ws, handoff.PublishShare(action="publish_share", key="DEMO-1", staged_id=STAGED))


def test_a_transient_error_keeps_the_last_known_addon_state():
    from orch_dashboard.gateway import Gateway

    class Known(AddonLink):
        welcome = {"workspace": {"id": "f" * 32}}

    on = Known([{"name": "publish", "state": "active"}])
    assert Gateway.addon_on(on, "publish") is True
    down = Known(HostError({"code": "host.unavailable"}, 3))
    assert Gateway.addon_on(down, "publish") is True  # kept, the host is only away for a moment
    off = Known([{"name": "publish", "state": "disabled"}])
    assert Gateway.addon_on(off, "publish") is False
    assert Gateway.addon_on(down, "publish") is False


def test_a_switch_session_cannot_register_and_the_route_takes_no_address(client, auth, stub, host):
    from orch_dashboard.policy import LOOPBACK

    switch = auth.open_switch(LOOPBACK, stub.ws)["token"]
    res = client.post(url(stub, "/shares/register"), json={"idem": IDEM}, headers=hdr(switch))
    assert res.status_code == 403 and res.json()["error"]["code"] == "session.readonly"
    assert not [c for c in host.calls if c[0] == "publish.register"]


def test_publish_addresses_are_checked_like_the_host_does(token):
    from pydantic import ValidationError

    for server, share in (
        ("https://a..example", "https://b.example"),
        ("https://a.example:99999", "https://b.example"),
    ):
        with pytest.raises(ValidationError):
            handoff.PublishSetup(action="publish_setup", server=server, share=share)


# -- apps (#1457): the signature that lets an app go live -------------------------------------------------------------

APP_STAGED = "pa_" + "e" * 32


def test_app_release_signs_exactly_the_hosts_words_for_this_staged_release():
    action = handoff.AppRelease(action="app_release", key="DEMO-1", staged_id=APP_STAGED)
    assert handoff.host_call(action) == ("app.release", {"staged_id": APP_STAGED})
    check = handoff.expected(action)
    text = "\n".join(
        [handoff.APP_MARK, "Ticket: DEMO-1", "Version: 2", f"Staged release: {APP_STAGED}", f"App id: {'ab' * 16}"]
    )
    assert check([{"type": "log.added", "text": text}]) is True
    assert check([{"type": "log.added", "text": text.replace(APP_STAGED, "pa_" + "f" * 32)}]) is False
    assert check([{"type": "log.added", "text": "hello\n" + text}]) is False  # does not start with the mark
    assert check([{"type": "log.added", "text": text.replace(handoff.APP_MARK, handoff.PUBLISH_MARK)}]) is False
    assert check([{"type": "ticket.updated", "text": text}]) is False
    assert check([{"type": "log.added", "text": text}, {"type": "log.added", "text": text}]) is False
    assert check([{"type": "log.added"}]) is False


def test_the_app_mark_is_the_cores_and_a_share_note_is_no_app_release():
    from orch.publishclient.approval import APP_MARK, MARK

    assert handoff.APP_MARK == APP_MARK != MARK


@pytest.mark.parametrize("staged", ["pa_xyz", "pa_" + "A" * 32, STAGED, SID, "../x"])
def test_app_release_needs_a_real_staged_release_id(staged):
    with pytest.raises(ValidationError):
        handoff.AppRelease(action="app_release", key="DEMO-1", staged_id=staged)


def test_app_release_is_a_click_op_in_core():
    from orch.click.protocol import op_class

    assert op_class("app.release") == "click"


def test_prepare_passes_work_folder_names_and_never_a_path(client, token, stub, host):
    host.answers["publish.prepare"] = PREPARED
    body = {"ticket": "DEMO-1", "workfiles": ["hello.txt"], "idem": IDEM}
    assert client.post(url(stub, "/shares/prepare"), json=body, headers=hdr(token)).status_code == 200
    assert host.calls[-1][1] == {"ref": "DEMO-1", "workfiles": ["hello.txt"], "kind": "secret", "days": 7}
    n = len(host.calls)
    for bad in (["../x"], ["a/b"], [".hid"], ["a.txt", "A.TXT"], [f"f{i}.txt" for i in range(21)], []):
        res = client.post(
            url(stub, "/shares/prepare"), json={"ticket": "DEMO-1", "workfiles": bad, "idem": IDEM}, headers=hdr(token)
        )
        assert res.status_code == 400, bad
    both = {"ticket": "DEMO-1", "artifacts": ["A.txt"], "workfiles": ["a.txt"], "idem": IDEM}
    assert client.post(url(stub, "/shares/prepare"), json=both, headers=hdr(token)).status_code == 400
    assert len(host.calls) == n
