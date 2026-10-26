"""Quick tasks and records (M6) against a REAL orch host: `quick.list` on seeded quick tasks, `records.status`, and a
records commit through the server's fixed child into a disposable repository.

Disposable everything (see `realhost_kit`): state, HOME and XDG_CONFIG_HOME are explicit and under a short throwaway
path with `.orch-dev` markers; the records child is given that HOME too, so it never reads the person's own files. The
"remote" is a LOCAL bare repository under the same path: no real remote is named, contacted or pushed to.
"""

import itertools
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from conftest import ORIGIN, PORT
from fastapi.testclient import TestClient
from realhost_kit import Workspace, short_run_root

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.gateway import Gateway

pytestmark = [
    pytest.mark.realhost,
    pytest.mark.skipif(os.name != "posix", reason="a real host needs unix sockets and a pty"),
]
RETRIES = itertools.count(5000)
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def seeding(ws: Workspace) -> dict:
    ws.agent("quick", "add", "Fix the typo in README")
    ws.agent("quick", "add", "Remove the dead import")
    ws.agent("new", "A normal ticket, not a quick task", "--type", "feature")
    ws.unattended("records", "init")  # the dedicated records repository at the workspace root
    return {}


def git(*args, cwd, check=True):
    # Local git only, with a disposable HOME so no person's configuration is read.
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
        # never discover an enclosing repository (the test HOME sits inside ~/orch-dev, itself a git repo): #1192
        "GIT_CEILING_DIRECTORIES": str(Path(cwd).resolve().parent),
    }
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if check:
        assert done.returncode == 0, done.stdout + done.stderr
    return done


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = short_run_root() / uuid.uuid4().hex[:6]
    ws = Workspace(root / "w")
    try:
        ws.bring_up(seeding)
    except (RuntimeError, TimeoutError, OSError) as exc:
        ws.stop_host()
        shutil.rmtree(root, ignore_errors=True)
        pytest.skip(f"the real host cannot start here: {str(exc)[:300]}")
    static = tmp_path_factory.mktemp("static")
    (static / "assets").mkdir()
    (static / "index.html").write_text('<html><script src="/assets/main-hash.js"></script></html>')
    (static / "assets/main-hash.js").write_text("console.log(1)")
    gateway = Gateway(ws.state)
    auth = Auth()
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as client:
        yield type("World", (), {"ws": ws, "gateway": gateway, "auth": auth, "client": client, "root": root})
    gateway.close()
    ws.stop_host()
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def token(world):
    launch = world.auth.launch(PORT)
    res = world.client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN})
    assert res.status_code == 200, res.text
    return res.json()["token"]


@pytest.fixture
def api(world, token, monkeypatch):
    # The records child inherits only HOME, PATH, SSH_AUTH_SOCK, LANG, XDG_CONFIG_HOME and the state dir.
    monkeypatch.setenv("HOME", str(world.ws.home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(world.ws.home / ".config"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    headers = {"Authorization": f"Bearer {token}"}

    class Api:
        base = f"/api/w/{world.ws.ws_id}"

        def get(self, path, **kw):
            return world.client.get(self.base + path, headers=headers, **kw)

        def post(self, path, body=None):
            if body is None:
                return world.client.post(self.base + path, headers={**headers, "Origin": ORIGIN})
            return world.client.post(self.base + path, json=body, headers={**headers, "Origin": ORIGIN})

        def handoff(self, body, timeout=60):
            res = self.post("/handoff", {**body, "retry": next(RETRIES)})
            assert res.status_code == 200, res.text
            prepared = res.json()
            world.ws.sign(prepared["code"])
            deadline = time.time() + timeout
            while time.time() < deadline:
                final = self.get(f"/handoff/{prepared['id']}").json()
                if final["state"] not in ("prepared", "bound"):
                    return prepared, final
                time.sleep(0.3)
            raise AssertionError("the request never reached a final state")

    return Api()


def test_the_host_advertises_quick_and_records(world, token):
    res = world.client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    features = res.json()["workspaces"][world.ws.ws_id]
    assert "quick" in features and "records" in features, features


def test_quick_list_is_the_seeded_quick_tasks_only(api):
    body = api.get("/quick").json()
    assert body["available"] is True
    assert sorted(r["title"] for r in body["rows"]) == ["Fix the typo in README", "Remove the dead import"]
    assert body["total"] == 2 and body["next_page"] is None
    for row in body["rows"]:
        assert row["labels"] == ["quick"] and row["type"] == "chore"
        # No repository is linked: the counts are unknown (null), never zero.
        assert row["quick"] == {"commits": None, "files": None, "over": None}


def test_quick_list_takes_the_flat_limits_and_the_status_filter(api):
    ok = api.get("/quick", params={"max_commits": 2, "max_files": 5, "limit": 10, "status": "open"})
    assert ok.status_code == 200 and ok.json()["total"] == 2
    assert api.get("/quick", params={"status": "done"}).json()["rows"] == []
    assert api.get("/quick", params={"max_commits": 0}).status_code == 400  # out of range is refused, not clamped
    assert api.get("/quick", params={"limits": "1"}).status_code == 400  # the nested draft form is not a thing


def test_adding_a_quick_task_as_a_person_is_two_signatures(api):
    prepared, final = api.handoff({"action": "new", "title": "Add a newline at the end of seeds", "type": "chore"})
    assert final["state"] == "submitted", final
    rows = api.get("/quick").json()["rows"]
    assert "Add a newline at the end of seeds" not in [r["title"] for r in rows]  # created, not yet labelled
    key = api.get("/tickets", params={"q": "Add a newline at the end of seeds"}).json()["rows"][0]["key"]
    _, final = api.handoff({"action": "details", "key": key, "labels": ["quick"]})
    assert final["state"] == "submitted", final
    rows = api.get("/quick").json()["rows"]
    assert key in [r["key"] for r in rows] and len(rows) == 3


def test_records_status_before_any_commit(api):
    body = api.get("/records").json()
    assert body["available"] is True
    # `configured` (core #454) means the workspace's records repository exists, as a tool last reported it: the
    # fixture's `orch records init` did. Nothing is committed or noted for the caller yet. The fixture's workspace is
    # registered with the running host (orch setup, V3-D38), so `records init` is served by the host and its repository
    # already reports a healthy state (before, a direct-mode init left `repo` None until the first commit).
    assert body["configured"] is True and body["last"] is None
    assert body["repo"] == {"state": "ok", "problem": None, "problem_hidden": False}
    assert body["pending"]["events"] > 0 and body["pending"]["more"] is False
    assert {t["key"] for t in body["pending"]["tickets"]} >= {"DEMO-0001", "DEMO-0002"}
    assert body["remote"] is None and body["auto"] is None


def test_a_commit_into_the_disposable_records_repository(world, api):
    res = api.post("/records/commit", {"push": False})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["ok"] is True and SHA.match(out["commit"]) and not out.get("pushed")
    log = git("log", "-1", "--format=%H %s", cwd=world.ws.root / ".orch").stdout.strip()
    assert log.startswith(out["commit"]) and re.search(r"orch: records .*ws_seq \d+$", log), log
    status = api.get("/records").json()
    assert (
        status["configured"] is True and status["repo"]["state"] == "ok" and status["repo"]["problem_hidden"] is False
    )
    assert status["last"]["commit"] == out["commit"] and status["last"]["pushed_at"] is None
    assert status["pending"]["events"] == 0
    # The same body twice is the same answer: nothing new is pending (the host says so, the child refuses or no-ops).
    again = api.post("/records/commit", {"push": False})
    assert again.status_code in (200, 409), again.text


def test_the_body_is_strict_and_auto_off_takes_none(api):
    assert api.post("/records/commit", {"push": "false"}).status_code == 400
    assert api.post("/records/commit", {"push": False, "extra": 1}).status_code == 400
    assert api.post("/records/commit").status_code == 400
    off = api.post("/records/auto-off")
    assert off.status_code == 200 and off.json()["ok"] is True  # nothing was on; switching it off is not an error


def test_a_push_to_a_local_bare_remote_is_refused_and_publishes_nothing(world, api):
    bare = world.root / "remote.git"
    git("init", "--bare", str(bare), cwd=world.root)
    git("remote", "add", "origin", str(bare), cwd=world.ws.root / ".orch")
    world.ws.agent("new", "Something new to commit", "--type", "chore")  # a commit needs something pending
    res = api.post("/records/commit", {"push": True})
    # A local path is not an allowed push protocol (https and ssh only) and a first push needs --initial:
    # either way the dashboard shows a stable code and the bare repository stays empty.
    assert res.status_code >= 400, res.text
    err = res.json()["error"]
    assert re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*", err["code"]), err
    refs = git("for-each-ref", cwd=bare).stdout.strip()
    assert refs == ""
    # The status is only what the host was told; whatever it says about the remote is a string, never a guess.
    remote = api.get("/records").json()["remote"]
    assert remote is None or remote["url"] in (None, str(bare))
