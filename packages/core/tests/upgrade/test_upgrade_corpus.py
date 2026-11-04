"""P1-E: the upgrade test. Frozen data of every released format must open with the code at HEAD.

``corpus/<kind>-f<N>/`` is the data a release of format N wrote (host state dir, workspace store), committed and never
edited (``make_corpus.py`` makes a new directory at a format change). For every committed corpus this copies it to a
temp dir, runs the registered migration steps from its format to ``CURRENT[kind]`` (``orch.migrate.plan`` / ``apply``,
what ``orch migrate --apply`` runs per root), opens it through ``open_root`` and checks the CONTENT (a workspace's
event chain and signatures, a host's registry, pins and key). Before the first release there is no step: format 1 is
the baseline and the plan is empty.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.custody import FileBackend
from orch.host.custody import read_json
from orch.host.registry import Entry, registry_path
from orch.migrate import CURRENT, apply, open_root, plan, read_format, write_format
from orch.ops.errors import OrchError
from orch.store import Store, checkpoints
from orch.store.pins import HostPins

pytestmark = pytest.mark.slow

CORPUS = Path(__file__).resolve().parent / "corpus"
KINDS = ("host", "workspace")
NAME = re.compile(rf"({'|'.join(KINDS)})-f([1-9][0-9]*)")


def corpora() -> list[tuple[str, int, Path]]:
    found = []
    for d in sorted(CORPUS.iterdir()) if CORPUS.is_dir() else []:
        m = NAME.fullmatch(d.name)
        if m and d.is_dir():
            found.append((m.group(1), int(m.group(2)), d))
    return found


CASES = [pytest.param(k, n, d, id=d.name) for k, n, d in corpora()]


def stage(src: Path, dst: Path) -> Path:
    """A private copy as a checkout gives it: git keeps no modes (the root's own checks want what orch writes: dirs
    0700, files 0600) and no empty folder."""
    shutil.copytree(src, dst)
    for p in sorted(dst.rglob("*"), reverse=True):
        if p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    for rel in (dst / "empty-dirs.txt").read_text().splitlines():  # the empty folders the data has (make_corpus.py)
        (dst / rel).mkdir(parents=True, exist_ok=True)
    for p in [dst, *dst.rglob("*")]:
        if p.is_symlink():
            raise AssertionError(f"a corpus holds no links: {p}")
        os.chmod(p, 0o700 if p.is_dir() else 0o600)
    return dst


def env_of(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home"), "ORCH_STATE_DIR": str(tmp_path / "state")}  # no update journal there


def migrate_and_open(kind: str, fmt: int, root: Path, env: dict[str, str]) -> None:
    assert read_format(root, kind) == fmt
    p = plan(root, kind)
    assert (p.at, p.target) == (fmt, CURRENT[kind])
    assert [s.from_n for s in p.steps] == list(range(fmt, CURRENT[kind]))  # one step per format, none at the baseline
    assert [s.name for s in p.steps] == apply(root, kind)
    assert read_format(root, kind) == CURRENT[kind] and not plan(root, kind).pending
    assert apply(root, kind) == []  # a second run is a no-op
    with open_root(root, kind, env) as handle:
        assert (handle.kind, handle.format) == (kind, CURRENT[kind])


def test_every_released_format_has_its_corpus() -> None:
    """The frozen corpora are the contract: removing one hides a break. Format 1 exists for every kind, and the name
    of each directory says the format its ``format.json`` holds."""
    have = {(k, n) for k, n, _ in corpora()}
    assert all((k, 1) in have for k in KINDS), have
    for k, n, d in corpora():
        assert read_format(d, k) == n, d
        assert n <= CURRENT[k], f"{d.name} is newer than this code: the corpus of an unreleased format"
        line = (d / "CORPUS.md").read_text()
        assert line.startswith(f"{d.name}: made by ") and re.search(r"at commit [0-9a-f]{40}", line), d


@pytest.mark.parametrize(("kind", "fmt", "corpus"), CASES)
def test_the_corpus_opens_with_head(kind: str, fmt: int, corpus: Path, tmp_path: Path) -> None:
    env = env_of(tmp_path)
    if kind == "workspace":
        project = tmp_path / "project"
        root = stage(corpus, project / ".orch")
        os.chmod(project, 0o700)
        migrate_and_open(kind, fmt, root, env)
        verify_workspace(project, root, env)
    else:
        root = stage(corpus, tmp_path / "state")
        migrate_and_open(kind, fmt, root, env)
        verify_host(root)


@pytest.mark.parametrize("kind", KINDS)
def test_a_newer_format_is_refused(kind: str, tmp_path: Path) -> None:
    """The self-test of the test: the same copy, marked format 2 (newer than this code), is refused by every door and
    nothing in it is changed."""
    root = stage(CORPUS / f"{kind}-f1", tmp_path / "root")
    write_format(root, kind, CURRENT[kind] + 1)

    def files() -> dict[Path, bytes]:  # the lock file is made by taking a lock, it holds nothing
        return {
            p.relative_to(root): p.read_bytes()
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != "format.lock"
        }

    before = files()
    for door in (lambda: plan(root, kind), lambda: apply(root, kind), lambda: open_root(root, kind, env_of(tmp_path))):
        with pytest.raises(OrchError) as e:
            door()
        assert e.value.code == "format.newer"
    assert files() == before


# ----------------------------------------------------------------------------------------------------- content


def read_log(path: Path) -> list[dict]:
    return [canon.parse_event_line(line) for line in path.read_bytes().splitlines(keepends=True)]


def verify_workspace(project: Path, store_dir: Path, env: dict[str, str]) -> None:
    ws = json.loads((store_dir / "config.json").read_bytes())["workspace"]["id"]
    logs = {"workspace": read_log(store_dir / "events" / "workspace.jsonl")}
    for d in sorted((store_dir / "tickets").iterdir()):
        logs[d.name] = read_log(d / "events.jsonl")
    genesis = logs["workspace"][0]
    assert genesis["type"] == "workspace.created" and genesis["workspace_id"] == ws
    wsk_pub = crypto.unb64u(genesis["wsk_pub"], crypto.PUB_LEN)
    dk_pub = crypto.unb64u(genesis["device_cert"]["o"]["dk_sig_pub"], crypto.PUB_LEN)

    # the model code over the stored bytes: canonical lines, an unbroken chain, every host_sig and every person sig
    types = {log: [e["type"] for e in events] for log, events in logs.items()}
    assert types["workspace"][:2] == ["workspace.created", "grant.issued"]
    (ticket_types,) = (t for log, t in types.items() if log != "workspace")
    assert ticket_types == ["ticket.created", "ticket.updated", "ticket.closed"]
    for log, events in logs.items():
        heads = canon.check_chain(events)
        for e in events:
            host_sig = crypto.unb64u(e["host_sig"], 64)
            assert crypto.verify(wsk_pub, host_sig, canon.host_signing_bytes(ws, log, e)), (log, e["seq"])
            if "sig" in e:  # person events: the one device of the genesis
                sig = crypto.unb64u(e["sig"], 64)
                assert crypto.verify(dk_pub, sig, canon.person_signing_bytes(ws, log, e)), (log, e["seq"])
        name = "workspace.json" if log == "workspace" else f"ticket-{log}.json"
        cp = json.loads((store_dir / ".state" / "checkpoints" / name).read_bytes())
        assert checkpoints.verify_object(wsk_pub, cp)
        if log != "workspace":
            assert cp["o"]["head"] == heads[-1] and cp["o"]["seq"] == len(events)

    # and the store's own reader opens it, read-only: every log replays and nothing is flagged
    store = Store.open(project, expected_workspace_id=ws, load="all")
    try:
        assert store.chain_errors() == [] and store.diverged == {} and store.reports == []
        (ticket,) = store.state.tickets.values()
        assert ticket.key == "DEMO-0001" and ticket.fields["title"] == "Corpus ticket, edited"
    finally:
        store.close()
        store.fs.close()


def verify_host(state: Path) -> None:
    registry = read_json(registry_path(state), os.getuid())
    assert registry["v"] == 1
    (entry,) = (Entry.from_doc(w) for w in registry["workspaces"])
    host = state / "hosts" / entry.workspace_id
    pins = HostPins(state, entry.workspace_id)
    assert pins.genesis() is not None and pins.checkpoint_evidence()  # the pin parses; the high-water mark is there
    key = FileBackend(host / "keys")
    wsk_pub = key.public_key("wsk")
    cp = json.loads((host / "checkpoint.json").read_bytes())
    assert cp["o"]["workspace_id"] == entry.workspace_id and cp["o"]["genesis"] == pins.genesis()
    assert checkpoints.verify_object(wsk_pub, cp)  # the host's key signed its own checkpoint
    assert stat.S_IMODE((host / "keys").stat().st_mode) == 0o700
    db = sqlite3.connect(f"file:{host / 'heads.sqlite'}?mode=ro", uri=True)
    try:
        assert db.execute("SELECT count(*) FROM heads").fetchone()[0] >= 2  # the genesis and the first grant
    finally:
        db.close()
