"""H11c: ``agent.start`` creates the ticket's worktree (§12.4.4, H11d's create under the per-repo lock) and starts the
agent in it; ``agent.preview`` never creates anything. Reuses H11d's world (a real repository, a real reader)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.host.control_client import new_idem
from orch.identity import new_ulid
from orch.runner.config import parse
from tests.host.dispatch_helpers import ok
from tests.host.test_agents import fake
from tests.host.test_runner_socket import wait_for
from tests.host.test_worktrees import w  # noqa: F401 - the H11d world fixture
from tests.store.helpers import GRANT_SECRET, ts

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="the runner is POSIX only"),
]


def _event(world, typ: str, **payload) -> None:
    hws = world.hws
    hws.tick()
    world.served.append(lambda store: store.append(hws.person_event(hws.owner, "workspace", typ, **payload),
                                               log="workspace"))  # fmt: skip


def test_start_creates_the_worktree_and_the_agent_runs_in_it(w, tmp_path):  # noqa: F811
    _event(w, "addon.granted", name="start-agent", version="0.1.0", package_sha256="sha256:" + "0" * 64,
           capabilities=["spawn_agent"], fields={}, sections=[], artifact_kinds=[])  # fmt: skip
    programs = {"codex": str(fake(tmp_path, "codex"))}
    doc = {"v": 1, "login_shell": False, "programs": programs}
    cfg = Path(w.served.lab.state) / "runner.json"
    cfg.write_text(json.dumps(doc))
    cfg.chmod(0o600)
    w.runner.link._config = parse(doc)
    gid = "gr_" + new_ulid()
    issued = w.hws.clock[0] + 1
    _event(w, "grant.issued", grant=gid, scope="workable", verbs="agent", issued_at=ts(issued), hours=8,
           expires_at=ts(issued + 8 * 3600), secret_hash=canon.grant_secret_hash(GRANT_SECRET),
           label="runner")  # fmt: skip
    value = f"{gid}.{crypto.b64u(GRANT_SECRET)}"
    c = w.c
    assert ok(c.req("grant.deposit", {"deposit_id": new_ulid()}, grant=value))["data"]["deposit"] == "confirmed"
    args = {"ticket": w.other, "harness": "codex", "mode": "work"}
    pre = ok(c.req("agent.preview", args))["data"]
    path = Path(f"{w.repo}.worktrees") / f"{w.served.wid[:8]}-{w.other}"
    assert pre["blocked"] is None, pre["blocked"]
    assert pre["will_create_worktree"] is True and pre["cwd"] == {"kind": "worktree", "repo": "proj", "ticket": w.other}
    assert pre["cwd_path"] == str(path) and not path.exists()  # the preview created nothing
    assert ok(c.req("agent.preview", args))["data"]["spec"] == pre["spec"]
    run = ok(c.req("agent.start", {**args, "spec": pre["spec"]}, idem=new_idem()))["data"]["run"]
    assert run["cwd"] == pre["cwd"] and run["kind"] == "agent"
    out = tmp_path / "out"
    wait_for(lambda: (out / "codex.env").exists())
    env = dict(line.split("=", 1) for line in (out / "codex.env").read_text().splitlines() if "=" in line)
    assert env["ORCH_WORKTREE"] == str(path) and os.path.samefile(env["PWD"], path)
    assert "ORCH_WORKDIR" not in env  # a ticket with a codebase has no work folder
    assert env["ORCH_GRANT"] == value
    # the worktree is outside the project: the walk finds nothing there, ORCH_WORKSPACE (named by the host) does
    from orch import discovery

    root = str(w.served.lab.root)
    assert env["ORCH_WORKSPACE"] == str(Path(w.served.lab.root))
    assert discovery.locate(path, explicit=False) is None
    found = discovery.locate(env["ORCH_WORKSPACE"], explicit=True)
    assert found is not None and found.config["workspace"]["id"] == w.served.wid
    # AGENTS.orch.md is in the project's store, not in the worktree: the prompt names it by absolute path
    assert pre["argv"][-1].endswith(f"Follow {os.path.join(root, '.orch', 'AGENTS.orch.md')}.") or pre["argv"][
        -1
    ].endswith(f"Follow {os.path.join(root, 'AGENTS.orch.md')}.")
    again = ok(c.req("agent.preview", args))["data"]  # the worktree is now registered: used, not created
    assert again["will_create_worktree"] is False and again["cwd_path"] == str(path)
    no = ok(c.req("agent.preview", {**args, "worktree": False}))["data"]
    assert no["cwd"] == {"kind": "repo", "repo": "proj"} and any("shared working copy" in x for x in no["warnings"])
