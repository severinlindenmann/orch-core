"""``quick.list`` on the workspace socket (H11f, #308, D-P2-35; contract ``quick/1``): quick tasks are tickets labelled
``quick``; the op equals ``ticket.query {label:["quick"]}`` for every viewer and adds commit and file counts from the
host reader. The size limits are display only; there is no quick-task store and no quick-task event."""

from __future__ import annotations

import base64
import errno
import json
import re
import threading
import time

import pytest

from orch.gitread.errors import Unavailable
from orch.host.dispatch import served_ops
from orch.ops import quick as core
from orch.ops.caps import Capabilities
from orch.ops.diff_core import diff_result
from tests.host.dispatch_helpers import SESSION, err, ok
from tests.host.lifecycle_helpers import Lab
from tests.host.live_helpers import World
from tests.host.test_dispatch_matrix import as_agent_class

pytestmark = pytest.mark.slow  # a host and git reader children

LINKS = 'links={"repos":["proj"],"branches":{"proj":"feat/x"}}'


def grant_quick(ws) -> None:
    """The Quick tasks add-on granted (it is off in a workspace without the grant)."""
    ws.tick()
    ws.store.append(
        ws.person_event(
            ws.owner, "workspace", "addon.granted", name="quick", version="0.1.0",
            package_sha256="sha256:" + "0" * 64, capabilities=[], fields={}, sections=[], artifact_kinds=[],
        ),
        log="workspace",
    )  # fmt: skip


@pytest.fixture
def world(tmp_path):
    lab = Lab(tmp_path.resolve())
    ws = lab.ws
    ws.store = ws.other()
    try:
        proj = ws.repo("proj")
        ws.git("branch", "main", "HEAD")
        (proj / "impl.txt").write_text("fixed\n")
        ws.git("commit", "-qam", "fix")
        (proj / "b.txt").write_text("b\n")
        ws.git("add", "b.txt")
        ws.git("commit", "-qm", "second")
        ws.tick()
        repos = {"proj": {"path": str(proj)}}
        ws.store.append(
            ws.person_event(ws.owner, "workspace", "settings.changed", set={"repos": repos}), log="workspace"
        )
        grant_quick(ws)
    finally:
        ws.store.close()
        ws.store = None
    try:
        served = World(lab)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    try:
        c = served.client()
        tickets = {}
        for name, pairs in (
            ("linked", ["labels=quick", LINKS]),
            ("bare", ["labels=quick"]),
            ("secret", ["labels=quick"]),
            ("plain", ["labels=other"]),
        ):
            uid, key = served.ticket(f"the {name} ticket")
            ok(c.req("set", {"ref": key, "pairs": pairs}, grant=served.grant, session=SESSION, idem=True))
            tickets[name] = (uid, key)
        served.restrict(tickets["secret"][0], [lab.owner])
        yield served, tickets
    finally:
        served.close()
        lab.close()


def quick_list(served, args=None, *, grant=None):
    return served.client().req("quick.list", args or {}, grant=grant, session=SESSION if grant else None)


def query(served, *, grant=None):
    res = served.client().req("ticket.query", {"label": ["quick"], "sort": "key", "order": "desc", "limit": 200},
                              grant=grant, session=SESSION if grant else None)  # fmt: skip
    return ok(res)["data"]


def direct_counts(served, uid):
    store = served.lab.ws.other()
    try:
        return core.counts(diff_result(Capabilities.direct({}), store, store.ticket(uid), context=0, max_bytes=1024))
    finally:
        store.close()


def test_quick_list_is_served_and_listed(world):
    served, _ = world
    welcome = served.client().welcome
    assert welcome["ops"]["quick.list"] == 1 and "quick/1" in welcome["caps"]
    assert "quick.list" in served_ops()


def test_quick_list_equals_ticket_query_for_the_label_and_adds_counts(world):
    served, tickets = world
    got = ok(quick_list(served))["data"]
    want = query(served)
    assert [r["key"] for r in got["rows"]] == [r["key"] for r in want["rows"]]
    assert got["total"] == want["total"] == 3 and got["next_page"] is None
    assert tickets["plain"][1] not in {r["key"] for r in got["rows"]}
    for row, base in zip(got["rows"], want["rows"], strict=True):
        assert {k: v for k, v in row.items() if k != "quick"} == base  # the full ticket.query row
    by_key = {r["key"]: r["quick"] for r in got["rows"]}
    commits, files = direct_counts(served, tickets["linked"][0])
    assert (commits, files) == (2, 2)
    assert by_key[tickets["linked"][1]] == {"commits": 2, "files": 2, "over": True}  # defaults: 1 commit, 3 files
    assert by_key[tickets["bare"][1]] == {"commits": None, "files": None, "over": None}  # no repo linked
    assert by_key[tickets["secret"][1]] == {"commits": None, "files": None, "over": None}


def test_the_limits_are_arguments_and_display_only(world):
    served, tickets = world
    key = tickets["linked"][1]

    def over(**limits):
        rows = ok(quick_list(served, limits))["data"]["rows"]
        return next(r["quick"]["over"] for r in rows if r["key"] == key)

    assert over() is True and over(max_commits=2) is False and over(max_commits=2, max_files=1) is True
    assert err(quick_list(served, {"max_commits": 0})) == "invalid.input"
    assert err(quick_list(served, {"max_files": 1001})) == "invalid.input"


def test_text_lines_are_the_ones_direct_mode_renders(world):
    served, tickets = world
    res = quick_list(served)
    assert any(tickets["linked"][1] in line and "2c/2f over" in line for line in res["result"]["lines"])
    assert res["result"]["hints"]


def test_paging_filters_and_the_dashboard_arguments(world):
    served, _ = world
    first = ok(quick_list(served, {"limit": 2}))["data"]
    assert len(first["rows"]) == 2 and first["total"] == 3 and first["next_page"]
    second = ok(quick_list(served, {"limit": 2, "page": first["next_page"]}))["data"]
    assert len(second["rows"]) == 1 and second["next_page"] is None
    assert {r["key"] for r in first["rows"] + second["rows"]} == {r["key"] for r in query(served)["rows"]}
    assert ok(quick_list(served, {"status": ["done"]}))["data"]["rows"] == []
    assert (
        err(quick_list(served, {"page": "bogus"})) == "page.expired"
    )  # not a token of this host (direct mode: invalid.input)
    assert err(quick_list(served, {"limit": 2, "page": first["next_page"], "status": ["open"]})) == "invalid.input"
    for bad in ({"limit": 0}, {"limit": 201}, {"status": []}, {"status": ["nope"]}, {"label": ["x"]}, {"sort": "key"}):
        assert err(quick_list(served, bad)) == "invalid.input", bad
    c = served.client()
    assert err(c.req("quick.list", {}, dry_run=True)) == "invalid.input"
    assert err(c.req("quick.list", {}, files={"x": {"b64": base64.b64encode(b"x").decode()}})) == "blob.invalid"


def test_a_restricted_ticket_is_never_listed_or_counted(world):
    """Column N (no person) and a grant whose person is not on the list see ``workspace`` tickets only; the answer
    equals ``ticket.query`` for the same viewer and says nothing of the restricted row."""
    served, tickets = world
    secret = tickets["secret"][1]
    assert secret in {r["key"] for r in ok(quick_list(served))["data"]["rows"]}  # the owner (P, on the list)
    served.restrict(tickets["secret"][0], [served.quinn.ref])
    got = ok(quick_list(served, grant=served.grant))["data"]  # G: the grant's person is the owner, not on the list
    assert secret not in {r["key"] for r in got["rows"]} and got["total"] == 2
    assert [r["key"] for r in got["rows"]] == [r["key"] for r in query(served, grant=served.grant)["rows"]]
    as_agent_class(served)  # N
    got = ok(quick_list(served))["data"]
    assert secret not in {r["key"] for r in got["rows"]} and got["total"] == 2
    assert [r["key"] for r in got["rows"]] == [r["key"] for r in query(served)["rows"]]
    assert "secret" not in str(got)


def test_a_reader_failure_gives_unknown_counts_not_an_error(world, monkeypatch):
    served, tickets = world
    reader = served.service.capabilities().git.reader

    def fail(self, *a, **k):
        raise Unavailable("the reader child died")

    monkeypatch.setattr(type(reader), "diff", fail)
    rows = ok(quick_list(served))["data"]["rows"]
    assert len(rows) == 3 and all(r["quick"] == {"commits": None, "files": None, "over": None} for r in rows)


def test_no_event_and_no_store_for_quick_tasks(world):
    """Listing writes nothing: the journal does not move."""
    served, _ = world
    before = served.snapshot_cursor("uid:" + next(iter(served.lab.peers())))
    ok(quick_list(served))
    assert served.snapshot_cursor("uid:" + next(iter(served.lab.peers()))) == before


QUICK_SCRIPT = [
    ("quick", "list"),
    ("quick", "add", "fix the typo"),
    ("quick", "add", "rename a flag", "--json"),
    ("new", "A feature"),
    ("new", "Q from new", "--label", "quick", "--type", "chore"),
    ("quick", "list"),
    ("quick", "list", "--json"),
    ("quick", "list", "--limit", "2"),
    ("quick", "list", "--status", "done"),
    ("quick", "list", "--max-commits", "5", "--max-files", "9", "--json"),
    ("quick", "add", "x" * 121),
    ("quick", "add", "two\nlines"),
    ("quick", "add"),
    ("show", "2", "--json"),
]


def test_quick_commands_render_identically_in_direct_and_host_mode(tmp_path_factory):
    """``quick list`` is the host's op in host mode and the registry handler in direct mode; ``quick add`` is the same
    two ordinary calls in both. Text and JSON are byte-identical (the host rows carry more ticket fields, so the JSON
    list is compared on the fields direct mode has)."""
    import json

    from tests.host.test_hostclient import WS, flat, hcli, hosted
    from tests.ops.helpers import Cli

    direct = Lab(tmp_path_factory.mktemp("d"))
    other = Lab(tmp_path_factory.mktemp("h"))
    try:
        d = Cli(direct.ws)
        want = [flat(d(*argv), WS) for argv in QUICK_SCRIPT]
        served = hosted(other)
        try:
            h = hcli(other)
            got = [flat(h(*argv), WS) for argv in QUICK_SCRIPT]
        finally:
            served.close()
    finally:
        direct.close()
        other.close()
    for argv, w, g in zip(QUICK_SCRIPT, want, got, strict=True):
        if argv[:2] == ("quick", "list") and "--json" in argv:
            wd, gd = json.loads(w[1])["data"], json.loads(g[1])["data"]
            keep = ("key", "title", "type", "status", "priority", "size", "labels", "owner", "quick")
            assert [{k: r[k] for k in keep} for r in gd["rows"]] == [{k: r[k] for k in keep} for r in wd["rows"]]
            assert (gd["total"], gd["next_page"]) == (wd["total"], wd["next_page"])
            assert (w[0], g[0]) == (0, 0) and w[2] == g[2]
        else:  # the page token is opaque and differs by mode
            tok = re.compile(r"--page \S+")
            assert (g[0], tok.sub("--page T", g[1]), g[2]) == (w[0], tok.sub("--page T", w[1]), w[2]), argv


def _slow_reader(monkeypatch, served, started, release, delay=None):
    reader = served.service.capabilities().git.reader
    real = type(reader).diff

    def slow(self, *a, **k):
        started.set()
        if delay is None:
            release.wait(20)
        else:
            time.sleep(delay)
        return real(self, *a, **k)

    monkeypatch.setattr(type(reader), "diff", slow)


def _in_thread(fn):
    box = {}

    def go():
        box["res"] = fn()

    t = threading.Thread(target=go)
    t.start()
    return t, box


def test_a_row_restricted_while_the_reader_runs_is_dropped_and_total_adjusts(world, monkeypatch):
    served, tickets = world
    started, release = threading.Event(), threading.Event()
    _slow_reader(monkeypatch, served, started, release)
    t, box = _in_thread(lambda: quick_list(served, grant=served.grant))
    try:
        assert started.wait(10)
        served.restrict(tickets["linked"][0], [served.quinn.ref])  # hidden from the grant's person meanwhile
    finally:
        release.set()
        t.join(30)
    got = ok(box["res"])["data"]
    assert tickets["linked"][1] not in {r["key"] for r in got["rows"]}
    assert (
        got["total"] == 2 and len(got["rows"]) == 2
    )  # three were read, this one dropped (the owner is on the secret's list)
    assert "linked ticket" not in str(box["res"])


def test_quick_list_shares_the_in_flight_slot_with_diff_and_counts_toward_the_host_cap(world, monkeypatch):
    import orch.host.diff as git_diff

    served, tickets = world
    started, release = threading.Event(), threading.Event()
    _slow_reader(monkeypatch, served, started, release)
    t, box = _in_thread(lambda: quick_list(served, grant=served.grant))
    try:
        assert started.wait(10)
        # the same caller's diff while its list runs: quota.host, retryable
        res = served.client().req("diff", {"ref": tickets["linked"][1]}, grant=served.grant, session=SESSION)
        assert err(res) == "quota.host" and res["error"]["retryable"] is True
        # another caller's diff: refused once the host-wide cap (MAX_CHILDREN - 2) is used up, the list holds one
        monkeypatch.setattr(git_diff, "MAX_DIFFS", 1)
        res = served.client().req("diff", {"ref": tickets["linked"][1]})
        assert err(res) == "quota.host"
    finally:
        release.set()
        t.join(30)
    ok(box["res"])
    ok(served.client().req("diff", {"ref": tickets["linked"][1]}))  # the slot is given back


def test_the_budget_gives_null_counts_to_the_rows_that_do_not_get_their_turn(world, monkeypatch):
    import orch.host.quick as host_quick

    served, tickets = world
    c = served.client()
    ok(c.req("set", {"ref": tickets["bare"][1], "pairs": [LINKS]}, grant=served.grant, session=SESSION, idem=True))
    monkeypatch.setattr(host_quick, "BUDGET_S", 1.0)  # room for a loaded CI machine to reach the first read
    _slow_reader(monkeypatch, served, threading.Event(), threading.Event(), delay=1.5)
    rows = ok(quick_list(served))["data"]["rows"]
    linked = [r for r in rows if r["key"] in (tickets["bare"][1], tickets["linked"][1])]
    assert [r["quick"]["commits"] is None for r in linked] == [
        False,
        True,
    ]  # the first read ran, the second had no time


def test_quick_add_never_labels_another_persons_chore_in_host_mode(tmp_path):
    """Quinn's untouched chore with the same title is not the owner's to label, and the owner's is not Quinn's."""
    from tests.host.test_hostclient import hcli
    from tests.ops.helpers import Cli

    lab = Lab(tmp_path.resolve())
    served = None
    try:
        lab.ws.store = lab.ws.other()
        grant_quick(lab.ws)
        lab.ws.store.close()
        lab.ws.store = None
        served = World(lab)
        quinn_grant = served.grant_for(served.quinn)
        mine = hcli(lab)
        theirs = Cli(lab.ws, ORCH_HOST_RUN_DIR=str(lab.run), ORCH_GRANT=quinn_grant)
        assert theirs("new", "same", "--type", "chore").code == 0
        r = mine("quick", "add", "same", "--json")
        doc = json.loads(r.out or r.err)
        assert "data" in doc, doc
        assert doc["data"]["created"] is True and doc["data"]["key"] == "DEMO-0002"
        assert mine("show", "1", "--json").out.count('"labels":[]') == 1  # quinn's ticket stays unlabelled
        assert theirs("new", "other", "--type", "chore").code == 0
        doc = json.loads(theirs("quick", "add", "other", "--json").out)  # theirs, retried by them: adopted
        assert doc["data"]["created"] is False and doc["data"]["key"] == "DEMO-0003"
        doc = json.loads(mine("quick", "add", "other", "--json").out)  # and not by the owner
        assert doc["data"]["created"] is True
    finally:
        if served is not None:
            served.close()
        lab.close()


def test_quick_label_is_refused_when_the_addon_is_off(tmp_path):
    lab = Lab(tmp_path.resolve())
    try:
        served = World(lab)
    except OSError as exc:  # pragma: no cover
        lab.close()
        if exc.errno == errno.EPERM:
            pytest.skip("Sandbox forbids AF_UNIX bind (EPERM)")
        raise
    try:
        c = served.client()
        uid, key = served.ticket("no grant")
        res = err(c.req("set", {"ref": key, "pairs": ["labels=quick"]}, grant=served.grant, session=SESSION, idem=True))
        assert res == "quick.off"
        ok(c.req("set", {"ref": key, "pairs": ["labels=other"]}, grant=served.grant, session=SESSION, idem=True))
    finally:
        served.close()
        lab.close()
