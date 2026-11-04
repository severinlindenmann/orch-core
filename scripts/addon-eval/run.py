#!/usr/bin/env python3
"""Add-on model eval on the real CLI.

  uv run --project packages/core python scripts/addon-eval/run.py --renderer today,proposed --set dev,heldout \\
      --n 5,12 --reps 3 --model claude-haiku-5-5 --tag base1 --out results.jsonl

Writes one JSON line per run to --out (resumable: finished runs are skipped) and a markdown table next to it.
Run it under `uv run --project packages/core` so `orch.instructions` is importable (it renders the real files).
`--probe` prints what the child `claude` can see (skills, tools, plugins, MCP servers) to check the isolation.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import addons as A  # noqa: E402
import build as B  # noqa: E402
import renderers as R  # noqa: E402
import report as REP  # noqa: E402
import tasks as T  # noqa: E402

lock = threading.Lock()
CORE_PKG = os.path.abspath(os.path.join(HERE, "..", "..", "packages", "core"))
CLAUDE_TOOLS = "Bash,Read,Edit,Write,Glob,Grep,Skill"
#: Allowlist instead of bypassPermissions: in -p mode with permission-mode default, anything not listed is denied.
#: Bash only runs the stub orch and a few read-only shell commands; Edit/Write stay inside the run workspace (cwd).
ALLOWED_TOOLS = ["Bash(orch:*)", "Bash(cat:*)", "Bash(ls:*)", "Bash(grep:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
                 "Bash(find:*)", "Bash(echo:*)", "Bash(pwd)", "Bash(python3:*)", "Bash(git status:*)", "Bash(git log:*)",
                 "Bash(git diff:*)", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]


def child_env(work: str, log: str, meta: str, cache: str) -> dict:
    """The child's env: no ORCH_*/XDG_CONFIG_HOME/CLAUDECODE from the parent; the stub first on PATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORCH_") and k not in ("XDG_CONFIG_HOME", "CLAUDECODE")}
    env["PATH"] = B.STUB_DIR + ":" + env["PATH"]
    renv = B.real_env(work)
    env.update(EVAL_LOG=log, EVAL_META=meta, EVAL_HELP_CACHE=cache, EVAL_REAL_ORCH=json.dumps(B.real_cmd(CORE_PKG)),
               EVAL_REAL_HOME=renv["HOME"], EVAL_REAL_PATH=renv["PATH"], CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    return env


def claude_cmd(model: str, prompt: str) -> list[str]:
    return ["claude", "-p", "--model", model, "--setting-sources", "project", "--strict-mcp-config", "--tools", CLAUDE_TOOLS,
            "--permission-mode", "default", "--allowedTools", ",".join(ALLOWED_TOOLS), "--max-turns", "15", "--output-format", "stream-json", "--verbose", prompt]


def parse_stream(raw: str):
    tools, text, turns, cost, skills_read, init = [], "", 0, 0.0, set(), {}
    for line in raw.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "system" and d.get("subtype") == "init":
            init = {k: d.get(k) for k in ("skills", "tools", "plugins", "mcp_servers", "slash_commands")}
        if d.get("type") == "assistant":
            for c in d["message"]["content"]:
                if c.get("type") == "tool_use":
                    tools.append((c["name"], c["input"]))
        if d.get("type") == "result":
            text = d.get("result") or ""
            turns = d.get("num_turns", 0)
            cost = d.get("total_cost_usd", 0.0)
    for n, i in tools:
        s = json.dumps(i)
        if n == "Skill" and str(i.get("skill", "")).startswith("orch-"):
            skills_read.add(i["skill"])
        elif "skills/orch-addon-" in s:
            skills_read.add(s.split("skills/orch-addon-")[1].split("/")[0].split('"')[0].split("\\")[0].split(" ")[0])
    return tools, text, turns, cost, sorted(skills_read), init


def one(job):
    renderer, set_name, n, t, rep, model, tag, work, out, cache = job
    tpl = os.path.join(work, "ws", f"{renderer}-N{n}")
    rd = os.path.join(work, "runs", f"{tag}-{model.split('-')[1]}-{renderer}-{set_name}-N{n}-{t['id']}-{rep}")
    shutil.rmtree(rd, ignore_errors=True)
    shutil.copytree(tpl, rd)
    ws, meta, log = rd + "/ws", rd + "/meta", rd + "/orch.log"
    open(log, "w").close()
    off = [x for x in t.get("stub_off", [])]  # rendered as active, but the stub answers addon.off (disabled mid-run)
    st = json.load(open(meta + "/state.json"))
    st["active"] = [a for a in st["active"] if a not in off]
    st["off"] = st["off"] + off
    st["flaky"] = list(t.get("stub_flaky", []))
    json.dump(st, open(meta + "/state.json", "w"))
    t0 = time.time()
    try:
        p = subprocess.run(claude_cmd(model, T.CTXS[set_name] + t["prompt"]), cwd=ws, env=child_env(work, log, meta, cache),
                           capture_output=True, text=True, timeout=300)
        raw = p.stdout
    except subprocess.TimeoutExpired as e:
        raw = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    open(rd + "/stream.jsonl", "w").write(raw)
    calls = [json.loads(l) for l in open(log) if l.strip()]
    tools, text, turns, cost, skills_read, _init = parse_stream(raw)
    grade = t["grader"](dict(calls=calls, tools=tools, text=text, ws=ws))
    rec = dict(tag=tag, model=model, renderer=renderer, set=set_name, n=n, task=t["id"], cell=t["cell"], rep=rep, grade=grade,
               turns=turns, cost=cost, secs=round(time.time() - t0, 1), skills_read=skills_read,
               read_agents_orch=any("AGENTS.orch.md" in json.dumps(i) for _, i in tools),
               calls=[c["argv"] + [f"rc={c['rc']}"] for c in calls], answer=text[:600], dir=rd)
    with lock:
        with open(out, "a") as f:
            f.write(json.dumps(rec) + "\n")
    return rec


def done_keys(out):
    s = set()
    if os.path.exists(out):
        for line in open(out):
            if line.strip():
                d = json.loads(line)
                s.add((d["tag"], d["model"], d["renderer"], d["set"], d["n"], d["task"], d["rep"]))
    return s


def probe(work, model):
    tpl, _ = B.build("proposed", 5, work)
    rd = os.path.join(work, "probe")
    shutil.rmtree(rd, ignore_errors=True)
    shutil.copytree(tpl, rd)
    log = rd + "/orch.log"
    open(log, "w").close()
    p = subprocess.run(claude_cmd(model, "Say OK.")[:-1] + ["Say OK."], cwd=rd + "/ws",
                       env=child_env(work, log, rd + "/meta", os.path.join(work, "help-cache")), capture_output=True, text=True, timeout=120)
    init = parse_stream(p.stdout)[5]
    local = sorted(os.listdir(rd + "/ws/.claude/skills"))
    others = [x for x in init.get("skills") or [] if x not in local]
    plugins = [p.get("source", str(p)) if isinstance(p, dict) else str(p) for p in init.get("plugins") or []]
    user_plugins = [p for p in plugins if not p.endswith("@builtin")]
    print("workspace skills:", local)
    print(f"other skills visible ({len(others)}, Claude Code's bundled ones as for any user):", ", ".join(others[:12]), "...")
    print("plugins:", plugins, "| mcp servers:", init.get("mcp_servers"), "| tools:", init.get("tools"))
    ok = not user_plugins and not init.get("mcp_servers") and all(x in (init.get("skills") or []) for x in local)
    print("ISOLATED (no user plugins, no MCP servers, workspace skills loaded)" if ok else "NOT ISOLATED: " + str(user_plugins))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--renderer", default="today,proposed")
    ap.add_argument("--set", default="dev,heldout", dest="sets")
    ap.add_argument("--n", default="5")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--tag", default="eval")
    ap.add_argument("--tasks", default="", help="comma list of task ids")
    ap.add_argument("--evals", default="", help="an add-on's evals.json (added as set `addon`)")
    ap.add_argument("--par", type=int, default=3)
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--dry", action="store_true", help="list the runs, call no model")
    a = ap.parse_args()
    work = B.work_dir()
    os.makedirs(os.path.join(work, "runs"), exist_ok=True)
    if a.evals:
        T.SETS["addon"] = T.load_evals(a.evals)
        T.CTXS["addon"] = T.CTX_HELDOUT
    cache = B.warm_help_cache(work, CORE_PKG)
    if a.probe:
        sys.exit(probe(work, a.model))
    renderers, ns = a.renderer.split(","), [int(x) for x in a.n.split(",")]
    for r in renderers:
        for n in ns:
            _, info = B.build(r, n, work)
            print(f"built {r} N={n}: {info}", flush=True)
    want = set(a.tasks.split(",")) if a.tasks else None
    done, jobs = done_keys(a.out), []
    for r in renderers:
        for st in a.sets.split(",") if not a.evals else ["addon"]:
            for n in ns:
                for t in T.tasks_for(st, A.ORDER[n]):
                    if want and t["id"] not in want:
                        continue
                    for rep in range(1, a.reps + 1):
                        if (a.tag, a.model, r, st, n, t["id"], rep) not in done:
                            jobs.append((r, st, n, t, rep, a.model, a.tag, work, a.out, cache))
    print(f"{len(jobs)} runs", flush=True)
    if a.dry:
        return
    with ThreadPoolExecutor(a.par) as ex:
        for rec in ex.map(one, jobs):
            print(rec["renderer"], rec["set"], rec["n"], rec["task"], rec["rep"], rec["grade"], rec["turns"], f"{rec['secs']}s", flush=True)
    rows = REP.load(a.out, a.tag, a.model)
    md = os.path.splitext(a.out)[0] + ".md"
    open(md, "w").write(REP.render(rows))
    print(f"wrote {md}")


if __name__ == "__main__":
    main()
