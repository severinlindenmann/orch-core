import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from devhost.host import StubHost
from fastapi.testclient import TestClient

from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.gateway import Gateway
from orch_dashboard.hostlink import HostLink

PORT = 43210
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture(autouse=True)
def no_signer_child(monkeypatch):
    """No test starts a signer child unless it says so: the child would read the person's own state directory."""
    monkeypatch.setenv("ORCH_DASHBOARD_CLICK", "off")
    for name in ("ORCH_GRANT", "ORCH_SESSION"):  # the launcher refuses under an agent's environment (#542)
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def desktop_not_headless(monkeypatch):
    """No test depends on the machine it runs on being a desktop or a headless server (CI's Linux runner has no
    display). Every server a test starts takes the random-port path; parallel workers must never meet on the fixed
    headless port, so binding it is a test failure. A test of the headless path passes its own environ."""
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setenv("DISPLAY", ":99")
    import orch_dashboard.__main__ as launcher

    real = launcher.bind_listener

    def guarded(wanted):
        assert wanted != launcher.HEADLESS_PORT, "a test must not bind the fixed headless port"
        return real(wanted)

    monkeypatch.setattr(launcher, "bind_listener", guarded)


@pytest.fixture
def click_start():
    """The signer's start function for the `gateway` fixture; a test that exercises click-to-sign overrides it."""
    return None


@pytest.fixture
def p1(tmp_path, monkeypatch):
    # Use core's signed-store test builder, not a dashboard mock model.
    from orch.addons.install import grant_event, package_dir
    from orch.addons.manifest import load_manifest
    from orch.addons.package import read_package

    from tests.store.helpers import Env

    env = Env(tmp_path)
    env.bootstrap()
    env.new_ticket("Real P1 ticket")
    manifest = Path(__file__).parents[2] / "orch-addon.json"
    directory = package_dir(env.store_dir, "dashboard")
    directory.mkdir(parents=True)
    (directory / "orch-addon.json").write_bytes(manifest.read_bytes())
    event = grant_event(load_manifest(manifest.read_bytes()), read_package(directory))
    typ = event.pop("type")
    env.store.append(env.person_event(env.owner, "workspace", typ, **event), log="workspace")
    (env.root / ".orch-dev").touch()
    monkeypatch.setenv("ORCH_DEV", "1")
    yield env
    env.store.close()


@pytest.fixture
def stub(p1):
    return StubHost(p1.root, p1.host_state, p1.owner.ref)


@pytest.fixture
def gateway(stub, p1, monkeypatch, click_start):
    directory = p1.host_state / "host"
    directory.mkdir(mode=0o700)
    path = directory / "workspaces.json"
    path.write_text(
        json.dumps(
            {
                "workspaces": [
                    {
                        "workspace_id": stub.ws,
                        "host_uid": os.getuid(),
                        "peers": {str(os.getuid()): {"class": "person", "person": stub.person}},
                    }
                ]
            }
        )
    )
    path.chmod(0o600)
    threads = []

    def connect(path, uid, ws):
        client, host = socket.socketpair()
        thread = threading.Thread(target=stub.serve_connection, args=(host, ws is None), daemon=True)
        thread.start()
        threads.append(thread)
        return HostLink(client, uid, ws)

    monkeypatch.setattr(HostLink, "connect", connect)
    from orch_dashboard.click_signer import Signer

    gateway = Gateway(p1.host_state, click=Signer(p1.host_state, start=click_start) if click_start else None)
    yield gateway
    gateway.close()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.fixture
def auth():
    return Auth()


@pytest.fixture
def client(gateway, auth, tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<html><head><script src="/assets/main-hash.js"></script></head></html>')
    (static / "assets/main-hash.js").write_text('console.log("public bundle")')
    with TestClient(create_app(PORT, gateway, auth, static), base_url=ORIGIN) as client:
        yield client


@pytest.fixture
def token(client, auth):
    launch = auth.launch(PORT)
    res = client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN})
    assert res.status_code == 200
    return res.json()["token"]


def git(path, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-C", str(path), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def rich(p1, stub):
    """A workspace with filled tickets, a question, evidence and a ticket branch in a real repository."""
    env = p1
    repo = env.tmp / "code"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "feat/one")
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / "b.txt").write_text("new\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "work")
    s = env.store
    s.append(
        env.person_event(
            env.owner,
            "workspace",
            "settings.changed",
            set={"repos": {"code": {"path": str(repo), "default_branch": "main"}}},
        ),
        log="workspace",
    )
    ids = {}
    for title, kind in [("Second", "bug"), ("Epic one", "epic"), ("Third", "feature")]:
        env.tick()
        ids[title] = env.new_ticket(title, kind)
    first = next(t.uid for t in s.state.tickets.values() if t.key == "DEMO-0001")
    ids["First"] = first
    env.tick()
    env.fill(first)
    env.tick()
    env.update(
        first,
        {
            "ticket.links": {"repos": ["code"], "branches": {"code": "feat/one"}, "prs": [], "external": []},
            "ticket.labels": ["api", "ui"],
            "ticket.priority": "high",
            "ticket.tasks": [
                {"id": "T1", "text": "Write the export", "verify": None, "proves": ["AC1"]},
                {"id": "T2", "text": "Document it", "verify": None, "proves": []},
            ],
        },
    )
    from orch.canon import hashing

    from tests.store.helpers import WS

    options = [{"key": "csv", "label": "Monthly CSV"}, {"key": "api", "label": "Tariff API"}]
    for n, (text, blocking) in enumerate([("Which export is the source?", True), ("Any naming rule?", False)], 1):
        qid = hashing.question_id(WS, first, f"Q{n}")
        question = {"id": f"Q{n}", "to": "ticket_owner", "text": text, "kind": "choice", "options": options,
                    "blocking": blocking}  # fmt: skip
        env.tick()
        s.append(
            {
                "type": "question.asked",
                "actor": env.agent,
                "question": question,
                "qid": qid,
                "hash": hashing.question_hash(qid, first, question),
            },
            log=first,
        )
        if n == 1:
            env.tick()
            s.append(
                env.person_event(
                    env.owner,
                    first,
                    "question.answered",
                    question="Q1",
                    hash=hashing.question_hash(qid, first, question),
                    option="csv",
                ),
                log=first,
            )
    from orch.store import observe

    observe.observe(s, first)
    env.tick()
    env.log(first, "hello\nworld")
    return env, ids


class DevBackend:
    """The dev workspace's device custody for tests: the owner's in-memory device key behind the Backend surface."""

    name = "passphrase"
    auth = "passphrase"
    person_capable = True

    def __init__(self, person):
        self.person = person
        self.signed = []

    def public_key(self, key_id):
        return self.person.sig_pub

    def sign(self, key_id, payload, action=""):
        self.signed.append(action)
        return self.person.dk_sign(payload)


class Signing:
    """A connection factory to the stub (a socket pair per link), the pins and the stub signer's seams."""

    def __init__(self, stub, p1, tmp_path):
        import shutil

        from orch.ops.runtime import _config  # noqa: F401  (the workspace config exists under p1.root)

        from tests.store.helpers import WS

        self.stub, self.p1, self.ws = stub, p1, WS
        keys = p1.host_state / "hosts" / WS / "keys"
        keys.parent.mkdir(parents=True, exist_ok=True)
        if not keys.exists():
            shutil.copytree(tmp_path / "wsk", keys)
        for path in (p1.host_state, p1.host_state / "hosts", keys.parent, keys):
            path.chmod(0o700)
        registry = p1.host_state / "host"
        registry.mkdir(mode=0o700, exist_ok=True)
        pin = {
            "workspace_id": WS,
            "root": str(p1.root),
            "host_uid": os.getuid(),
            "peers": {str(os.getuid()): {"class": "person", "person": stub.person}},
        }
        (registry / "workspaces.json").write_text(json.dumps({"workspaces": [pin]}))
        (registry / "workspaces.json").chmod(0o600)
        self.backend = DevBackend(p1.owner)
        self.threads = []

    def link(self):
        client, host = socket.socketpair()
        thread = threading.Thread(target=self.stub.serve_connection, args=(host, False), daemon=True)
        thread.start()
        self.threads.append(thread)
        return HostLink(client, os.getuid(), self.ws)

    def connect(self, path, uid, ws):
        return self.link()



@pytest.fixture
def signing(stub, p1, tmp_path):
    return Signing(stub, p1, tmp_path)


class World:
    """One dashboard server (its own session) for one person of the dev workspace."""

    def __init__(self, client, token, stub, gateway):
        self.client, self.token, self.stub, self.gateway = client, token, stub, gateway

    def headers(self, write=False):
        return {"Authorization": f"Bearer {self.token}", **({"Origin": ORIGIN} if write else {})}

    def get(self, path, **kw):
        return self.client.get(path, headers=self.headers(), **kw)

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers(True))

    def put(self, path, body):
        return self.client.put(path, json=body, headers=self.headers(True))


@pytest.fixture
def worlds(p1, tmp_path, monkeypatch):
    """`worlds(person_ref)` -> a `World`: the stub host and a dashboard server acting as that person (the owner's
    registry pin is replaced by the person's own, in a state directory of its own)."""
    routes: dict[str, StubHost] = {}
    threads, stack = [], []

    def connect(path, uid, ws):
        stub = next(s for prefix, s in routes.items() if str(path).startswith(prefix))
        client, host = socket.socketpair()
        thread = threading.Thread(target=stub.serve_connection, args=(host, ws is None), daemon=True)
        thread.start()
        threads.append(thread)
        return HostLink(client, uid, ws)

    monkeypatch.setattr(HostLink, "connect", connect)

    def make(person: str, **flags):
        stub = StubHost(p1.root, p1.host_state, person)
        for name, value in flags.items():
            setattr(stub, name, value)
        state = tmp_path / f"registry-{len(routes)}"
        (state / "host").mkdir(parents=True, mode=0o700)
        pin = {
            "workspace_id": stub.ws,
            "host_uid": os.getuid(),
            "peers": {str(os.getuid()): {"class": "person", "person": person}},
        }
        registry = state / "host/workspaces.json"
        registry.write_text(json.dumps({"workspaces": [pin]}))
        registry.chmod(0o600)
        routes[str(state / "run")] = stub
        gateway = Gateway(state)
        static = tmp_path / f"static-{len(routes)}"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<html></html>")
        app_client = TestClient(create_app(PORT, gateway, Auth(), static), base_url=ORIGIN)
        app_client.__enter__()
        stack.append((app_client, gateway))
        launch = app_client.app.state.auth.launch(PORT)
        res = app_client.post("/auth/session", json={"code": launch["code"]}, headers={"Origin": ORIGIN})
        assert res.status_code == 200
        return World(app_client, res.json()["token"], stub, gateway)

    yield make
    for app_client, gateway in stack:
        app_client.__exit__(None, None, None)
        gateway.close()
    for thread in threads:
        thread.join(timeout=3)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """The nightly realhost job (ORCH_REALHOST_STRICT=1) must not go green on skips: 'the real host cannot start here'
    is how a stale test or a broken setup hides (#587). Locally the skip stays, for machines without a pty."""
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("ORCH_REALHOST_STRICT") and report.skipped and "cannot start here" in str(report.longrepr):
        report.outcome = "failed"
        report.longrepr = (
            f"strict realhost run: {report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr}"
        )
