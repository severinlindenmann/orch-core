"""Build workspace templates: one directory per (renderer, N) under $EVAL_WORK/ws/<renderer>-N<n>/{ws,meta}."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile

import addons as A
import renderers as R

HERE = os.path.dirname(os.path.abspath(__file__))
STUB_DIR = os.path.join(HERE, "stub")


def work_dir() -> str:
    return os.environ.get("EVAL_WORK") or os.path.join(tempfile.gettempdir(), "addon-eval")


PROJECT_FILES = {
    "src/util.py": "def parse_cfg(path):\n    return dict(l.split('=', 1) for l in open(path) if '=' in l)\n\n\nif __name__ == '__main__':\n    print(parse_cfg('app.cfg'))\n",
    "app.cfg": "debug=1\n",
    "README.md": "# Invoicer\nRequires Python 3.9.\n![CI](https://ci.example.com/old-repo/badge.svg)\n",
    "wiki/rollback.md": "# Rollback of the payments service\n\n1. Page the on-call in #payments-oncall.\n2. Run `deployctl swap --to blue` to switch traffic back to the previous release (takes about 15 minutes).\n3. Verify with `deployctl health payments`.\n4. Never roll back during the nightly batch (02:00-03:00).\n",
    "wiki/deploy-notes.md": "# Deploy notes\n\nDeploys go out Tuesdays and Thursdays. Use `deployctl push` for staging only.\n",
    "tests/test_sync.py": "import random\n\n\ndef test_sync_flaky():\n    assert random.random() < 0.9\n",
}
SESSION_HOOK = "echo 'ok session-start alice · grant gr_01HX · claim DEMO-0007 in-progress (your claim) · T1 next'; echo 'next: orch show'"


def real_cmd(core_pkg: str) -> list[str]:
    """argv that runs the REAL orch CLI of this checkout (`uv run --project <packages/core> orch`)."""
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--project", core_pkg, "orch"]


def real_env(work: str) -> dict:
    """Clean env for the real CLI: own HOME, no ORCH_*/XDG, no workspace."""
    home = os.path.join(work, "real-home")
    os.makedirs(home, exist_ok=True)
    uvdir = os.path.dirname(shutil.which("uv") or "/usr/bin/uv")
    return {"HOME": home, "PATH": f"/usr/bin:/bin:{uvdir}"}


def warm_help_cache(work: str, core_pkg: str) -> str:
    """Pre-render the usual help/describe calls with the real CLI into the cache dir the stub reads. Returns the dir."""
    cache = os.path.join(work, "help-cache")
    os.makedirs(cache, exist_ok=True)
    from orch.ops.workflows import WORKFLOWS

    calls = [["help"], ["--help"], ["describe"], *[["help", w] for w in WORKFLOWS]]
    calls += [["describe", w] for w in ("quick", "follow-up", "set", "new", "status", "claim", "show", "submit", "handoff")]
    env = real_env(work)
    for args in calls:
        p = os.path.join(cache, hashlib.sha1("\x00".join(args).encode()).hexdigest() + ".json")
        if os.path.exists(p):
            continue
        r = subprocess.run(real_cmd(core_pkg) + args, env=env, capture_output=True, text=True, timeout=120)
        json.dump({"out": (r.stdout + r.stderr).rstrip("\n"), "rc": r.returncode}, open(p, "w"))
    return cache


def build(renderer: str, n: int, work: str | None = None) -> tuple[str, dict]:
    """Template for (renderer, N). Returns (template dir, info)."""
    work = work or work_dir()
    tpl = os.path.join(work, "ws", f"{renderer}-N{n}")
    ws, meta = os.path.join(tpl, "ws"), os.path.join(tpl, "meta")
    shutil.rmtree(tpl, ignore_errors=True)
    os.makedirs(ws)
    os.makedirs(meta)
    active = A.ORDER[n]
    files, info = R.render(renderer, active, A.OFF)
    files.update(PROJECT_FILES)
    for rel, text in files.items():
        path = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    os.makedirs(os.path.join(ws, ".claude"), exist_ok=True)
    json.dump({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": SESSION_HOOK}]}]}},
              open(os.path.join(ws, ".claude", "settings.json"), "w"), indent=1)
    json.dump({"active": active, "off": A.OFF}, open(os.path.join(meta, "state.json"), "w"))
    return tpl, info


if __name__ == "__main__":
    import sys

    for r in R.RENDERERS:
        for n in sorted(A.ORDER):
            _, info = build(r, n, sys.argv[1] if len(sys.argv) > 1 else None)
            print(r, n, info)
