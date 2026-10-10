"""Golden output of every C6 operation, text and JSON, for one fixed workspace built through the real store.

``uv run pytest tests/ops/test_golden.py --update-golden`` rewrites ``tests/ops/golden/``; without the flag a diff, a
missing file or a stale file fails. Each case runs on a fresh copy of the same workspace (a write cannot be run twice),
on the fixed test clock; what a run makes up on its own (the owner's person id, the grant id, the duration of a
receipt) is replaced by a name."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from tests.ops.helpers import OTHER, Cli, Ws

GOLDEN = Path(__file__).parent / "golden"
SEQ = {}


def build(tmp: Path) -> Ws:
    ws = Ws(tmp)
    ws.bootstrap()
    cli = Cli(ws)
    other = Cli(ws, session=OTHER)
    must = lambda r: (r.code == 0, r.err)[0] or pytest.fail(r.err)  # noqa: E731
    for argv in (
        [
            "new",
            "Load tariff tables as dbt seeds",
            "--priority",
            "high",
            "--label",
            "dbt,tariffs",
            "-m",
            "Load the 40 tariff tables as seeds.",
        ],
        ["claim", "DEMO-0001"],
        ["section", "set", "plan", "-m", "1. export the CSVs\n2. seed them"],
        ["ac", "add", "`dbt seed` loads all 40 tariff tables"],
        ["ac", "add", "The refresh command is documented"],
        ["task", "add", "Export CSVs into seeds/tariffs", "--verify", "echo exported 40", "--proves", "AC1"],
        ["task", "add", "Document the refresh command", "--proves", "AC2"],
        ["task", "add", "Join in fct_billing", "--verify", "echo joined"],
        ["task", "start", "T1"],
        ["task", "done", "T1", "--run", "-m", "40 files"],
        ["log", "Seeds are in; the join is next."],
        [
            "ask",
            "Which export is the source of truth?",
            "--options",
            "csv,api",
            "--rec",
            "csv",
            "--why",
            "They differ for 3 tariffs.",
        ],
        ["new", "Fix the billing join", "--type", "bug", "--priority", "urgent"],
        ["new", "Handed over", "--label", "from-peer,peer.acme"],
    ):
        must(cli(*argv))
    ev = Path(ws.tmp / "evidence.log")
    ev.write_text("112 passed\n")
    must(cli("artifact", "add", str(ev), "--ac", "AC1", "--label", "test run"))
    must(other("claim", "DEMO-0003"))
    return ws


@pytest.fixture(scope="module")
def base():
    root = Path(tempfile.mkdtemp(prefix="orch-golden-"))
    ws = build(root)
    (root / "grant.txt").write_text(ws.grant)
    ws.store.close()
    yield root
    shutil.rmtree(root, ignore_errors=True)


class Fork(Ws):
    """A copy of the base workspace, with the same ids and keys, on the same clock."""

    def __init__(self, src: Path, dst: Path) -> None:
        shutil.copytree(src, dst, symlinks=True)
        self.tmp, self.root, self.host_state = dst, dst / "workspace", dst / "host-state"
        self.clock = [1_790_000_000 + 400]
        self.live = False
        self.store = None
        self._grant = (src / "grant.txt").read_text()

    @property
    def grant(self) -> str:
        return self._grant  # type: ignore[return-value]


NORMAL = [
    (re.compile(r"p_[0-9a-f]{32}"), "p_PERSON"),
    (re.compile(r"gr_[0-7][0-9A-HJKMNP-TV-Z]{25}"), "gr_GRANT"),
    (re.compile(r"exit0/[0-9]+ms"), "exit0/Nms"),
    (re.compile(r'"ms":[0-9]+'), '"ms":N'),
    (re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}"), "ULID"),
]


def norm(text: str) -> str:
    for rx, to in NORMAL:
        text = rx.sub(to, text)
    return text


CASES = {
    "status": (["status"], None),
    "next": (["next"], None),
    "show": (["show"], None),
    "show-section": (["show", "--section", "summary,plan"], None),
    "show-full": (["show", "--full"], None),
    "show-log": (["show", "--log", "--since", "10"], None),
    "show-diff": (["show", "--diff", "--since", "12"], None),
    "list": (["list"], None),
    "list-mine": (["list", "--mine"], None),
    "search": (["search", "seed"], None),
    "inbox": (["inbox"], None),
    "new": (
        ["new", "A brand new ticket", "--type", "chore", "--priority", "low", "--label", "ops", "-m", "Why it exists."],
        None,
    ),
    "claim": (["claim", "DEMO-0002"], None),
    "claim-next": (["claim", "--next"], None),
    "claim-takeover": (["claim", "DEMO-0003", "--takeover", "--reason", "the first session stalled"], None),
    "claim-held": (["claim", "DEMO-0003"], None),
    "release": (["release"], None),
    "handoff": (["handoff", "-m", "T1 done; T2 is documentation; the join needs the API answer."], None),
    "submit": (["submit"], None),
    "ask": (["ask", "May I rename the seeds?", "--options", "yes,no", "--rec", "no"], None),
    "wait": (["wait", "--timeout", "1"], None),
    "wait-strict": (["wait", "--timeout", "1", "--strict-timeout"], None),
    "set": (["set", "DEMO-0001", "priority=urgent", "size=m", "labels=dbt,seeds"], None),
    "section-set": (["section", "set", "context", "-m", "Tariff tables come from the billing team."], None),
    "ac-add": (["ac", "add", "Row counts match the source"], None),
    "ac-edit": (["ac", "edit", "AC2", "The refresh command is documented in the README"], None),
    "task-list": (["task", "list"], None),
    "task-next": (["task", "next"], None),
    "task-add": (["task", "add", "Add tests", "--verify", "pytest -q", "--proves", "AC1"], None),
    "task-start": (["task", "start", "T2"], None),
    "task-done": (["task", "done", "T2", "-m", "documented"], None),
    "task-done-run": (["task", "done", "T3", "--run"], None),
    "task-skip": (["task", "skip", "T3", "--reason", "not needed"], None),
    "task-block": (["task", "block", "T3", "--reason", "needs the API key"], None),
    "task-reopen": (["task", "reopen", "T1", "--reason", "seeds changed"], None),
    "artifact-add": (["artifact", "add", "{evidence}", "--name", "second.log", "--kind", "log"], None),
    "artifact-replace": (["artifact", "replace", "evidence.log", "{evidence}"], None),
    "artifact-list": (["artifact", "list"], None),
    "log": (["log", "Waiting for the answer."], None),
    "apply": (
        ["apply", "--file", "-"],
        json.dumps({"ops": [{"op": "log", "text": "batch"}, {"op": "ac.add", "text": "Third criterion"}]}),
    ),
}


def run(base: Path, name: str, as_json: bool) -> str:
    argv, stdin = CASES[name]
    tmp = Path(tempfile.mkdtemp(prefix="orch-golden-run-"))
    try:
        ws = Fork(base, tmp / "w")
        ev = str(ws.tmp / "evidence.log")
        cli = Cli(ws)
        r = cli(*[a.replace("{evidence}", ev) for a in argv], *(["--json"] if as_json else []), stdin=stdin)
        if as_json:
            doc = json.loads(r.out)
            return norm(json.dumps(doc, indent=2, ensure_ascii=False)) + f"\n# exit {r.code}\n"
        return norm(f"exit {r.code}\n--- stdout\n{r.out}--- stderr\n{r.err}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.parametrize("name", sorted(CASES))
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_golden(name, as_json, base, request):
    text = run(base, name, as_json)
    path = GOLDEN / f"{name}.{'json' if as_json else 'txt'}"
    if request.config.getoption("--update-golden"):
        assert not os.environ.get("CI"), "--update-golden is refused when CI is set"
        path.parent.mkdir(exist_ok=True)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            print(f"golden written: {path.name}")
        return
    assert path.exists(), f"missing {path.name}; run pytest tests/ops/test_golden.py --update-golden -s"
    assert path.read_text() == text, f"{path.name} differs; run with --update-golden if intended"


def test_no_stale_golden_files(request):
    if request.config.getoption("--update-golden"):
        for p in GOLDEN.glob("*"):
            if p.stem not in CASES:
                p.unlink()
        return
    have = {p.name for p in GOLDEN.glob("*")}
    want = {f"{n}.{e}" for n in CASES for e in ("txt", "json")}
    assert have == want, (sorted(have - want), sorted(want - have))
