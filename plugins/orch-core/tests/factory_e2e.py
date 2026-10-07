"""A deterministic end-to-end harness for the Dark AI Factory chain (tests/test_factory_e2e.py).

Everything real but the agent, tmux and the clock: real git (a workspace checkout, a bare "remote", the runner's
clones), the runner's own round (factory_runner.tick) with a fake launcher, the release with its real executor and
the example release-step script, and a scripted agent per session. The agent runs the commands its built-in prompt
names (each taken from the prompt's own text), each through the real hook entry points as Claude Code calls them: the
PreToolUse guard, then the PermissionRequest hook (they share permits.bash_gate), in the session's own folder with the
environment the runner gives it; an allowed orch command runs through the real CLI (orch.cli.run), on the workspace
through ORCH_HOME, and an allowed git command runs for real in the session's clone.

What it cannot cover: what a real model makes of the prompt (a script runs exactly its steps), what Claude Code
itself allows without asking (here every shell command reaches the permission hook), and how a real tmux pane renders
(a pane here is one of a few fixed screens)."""
from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from orch import clock
from orch.cli import run as cli_run
from orch.core import factory_runner, factory_sessions as fs, permits, store
from orch.core.workspace import Workspace
from orch.hooks.guard import evaluate

BOX = "╭" + "─" * 40 + "╮\n│ >" + " " * 38 + "│\n╰" + "─" * 40 + "╯\n"
IDLE = "✳ Done.\n\n" + BOX + "  ⏵⏵ accept edits on (shift+tab to cycle)\n"
BUSY = IDLE.replace("accept edits on (shift+tab to cycle)", "Running… (esc to interrupt)")
TRUST = ("Accessing workspace:\n\n {path}\n\n Is this a project you trust?\n ❯ 1. Yes, I trust this folder\n"
         "   2. No, exit\n\n Enter to confirm · Esc to cancel\n")
GIT_ID = {"GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "agent@example.invalid", "GIT_COMMITTER_NAME": "agent",
          "GIT_COMMITTER_EMAIL": "agent@example.invalid"}
STOP = "stop"  # what a step returns to end the session's turn: it waits at its prompt for a nudge


@dataclass
class Call:
    """One tool call of a session and what came of it: `by` is what refused it (guard, hook), None when it ran."""
    tool: str
    text: str
    by: str | None = None
    message: str = ""
    code: int | None = None
    out: str = ""

    @property
    def ran(self) -> bool:
        return self.by is None


def snippets(prompt: str) -> list[str]:
    """The commands a built-in prompt names, in backticks."""
    return re.findall(r"`([^`]+)`", prompt)


def from_prompt(prompt: str, template: str, **subs: str) -> str:
    """`template` as the prompt names it (it must: the agent runs what it was told), its placeholders filled in."""
    assert template in snippets(prompt), f"the prompt does not name `{template}`"
    for k, v in subs.items():
        template = template.replace(k, v)
    return template


class Agent:
    """A runner-bound session's tool calls as Claude Code makes them: the guard first (PreToolUse); for a shell command
    then the permission hook (no allow rule at user scope, so every command prompts), each with the payload the
    harness sends. An allowed command runs in the session's folder with the session's environment."""

    def __init__(self, world, b: dict, prompt: str):
        self.world, self.b, self.prompt = world, b, prompt
        self.sid, self.cwd, self.child = b["session"], b["start"], b["child"]
        self.calls: list[Call] = []
        self.nudges: list[str] = []  # what the runner typed into this session's pane

    @contextlib.contextmanager
    def _env(self):
        with self.world.mp.context() as mp:
            mp.chdir(self.cwd)
            mp.setenv("ORCH_HOME", str(self.world.ws.home))  # what the runner sets (factory_runner._start)
            mp.setenv("CLAUDECODE", "1")
            mp.setenv("CLAUDE_CODE_SESSION_ID", self.sid)  # Claude Code exports it to its commands and hooks
            yield Workspace.open(self.cwd)  # how orch's hooks find the workspace from the session's folder

    def _payload(self, tool: str, inp: dict, event: str = "PreToolUse") -> dict:
        return {"session_id": self.sid, "tool_name": tool, "tool_input": inp, "cwd": self.cwd,
                "hook_event_name": event}

    def bash(self, command: str) -> Call:
        call = Call("Bash", command)
        with self._env() as ws:
            g = evaluate(ws, self._payload("Bash", {"command": command}))
            if not g.allow:
                call.by, call.message = "guard", g.reason
            else:
                h = permits.hook_decision(ws, self._payload("Bash", {"command": command}, "PermissionRequest"))
                d = (h or {}).get("hookSpecificOutput", {}).get("decision", {})
                if d.get("behavior") != "allow":  # None would be the harness asking a person: never in a run
                    call.by, call.message = "hook", d.get("message", "no answer from the permission hook")
                else:
                    call.code, call.out = self._exec(command)
        self.calls.append(call)
        return call

    def _exec(self, command: str) -> tuple[int, str]:
        words = shlex.split(command)
        if words[0] == "orch":
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = cli_run(words[1:])
            return code, out.getvalue()
        if words[0] == "git":
            r = subprocess.run(words, cwd=self.cwd, capture_output=True, text=True, env={**os.environ, **GIT_ID})
            return r.returncode, r.stdout + r.stderr
        raise AssertionError(f"the script ran a command the harness does not run: {command}")

    def write(self, path, text: str) -> Call:
        """The Write tool: only the guard judges it (accept-edits mode: no permission prompt for file edits)."""
        p = Path(self.cwd, path)
        call = Call("Write", str(p))
        with self._env() as ws:
            g = evaluate(ws, self._payload("Write", {"file_path": str(p), "content": text}))
        if g.allow:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        else:
            call.by, call.message = "guard", g.reason
        self.calls.append(call)
        return call

    def ok(self, command: str) -> Call:
        """A command the run needs: it must run and succeed."""
        c = self.bash(command)
        assert c.ran and c.code == 0, (self.child, command, c)
        return c

    def denied(self) -> list[Call]:
        return [c for c in self.calls if not c.ran]


class Script:
    """A session's work as named steps, one tool call each (a step returning STOP ends the turn: the session waits at
    its prompt until the runner types a nudge, which the script receives). Tests edit the steps by name to script a
    failure the live runs showed."""

    def __init__(self, agent: Agent, steps: list[tuple[str, object]]):
        self.agent, self.steps = agent, list(steps)

    def _at(self, name: str) -> int:
        return next(i for i, (n, _) in enumerate(self.steps) if n == name)

    def replace(self, name: str, *steps):
        i = self._at(name)
        self.steps[i:i + 1] = steps
        return self

    def before(self, name: str, *steps):
        i = self._at(name)
        self.steps[i:i] = steps
        return self

    def after(self, name: str, *steps):
        i = self._at(name) + 1
        self.steps[i:i] = steps
        return self

    def drop(self, *names: str):
        self.steps = [s for s in self.steps if s[0] not in names]
        return self

    def names(self) -> list[str]:
        return [n for n, _ in self.steps]

    def run(self):
        for _, fn in self.steps:
            if fn() == STOP:
                self.agent.nudges.append((yield STOP))
            else:
                yield


# -- the scripts: the planner and the worker as their built-in prompts say ------------------------------------------

def planner(agent: Agent, deliverables: list[str]) -> Script:
    """Read the epic, record the deliverables, create one child per file (its name in the criteria, from files written
    with the Write tool), a Plan each, then auto-approve each, and stop."""
    p, e, ws = agent.prompt, agent.child, agent.world.ws
    made: list[str] = []

    def new(i, name):
        c = agent.ok(from_prompt(
            p, f'orch new --epic {e} --title "..." --size SIZE --requirements-file FILE --acceptance-file FILE',
            **{'"..."': f'"Create {name}"', "SIZE": "s",
               "--requirements-file FILE": f"--requirements-file orchestrator/temporary/req-{i}.md",
               "--acceptance-file FILE": f"--acceptance-file orchestrator/temporary/ac-{i}.md"}))
        made.append(re.search(r"created ([A-Z]+-\d+)", c.out).group(1))

    steps = [("show", lambda: agent.ok(from_prompt(p, f"orch show {e}"))),
             ("limits", lambda: agent.ok(from_prompt(p, f"orch epic show {e}"))),
             ("list", lambda: agent.ok(from_prompt(p, f'orch log {e} -m "..."',
                                                   **{"...": "Deliverables: " + ", ".join(deliverables)})))]
    for i, name in enumerate(deliverables, 1):
        steps += [(f"requirements {i}", lambda i=i, name=name: agent.write(
                      ws.temporary_dir / f"req-{i}.md", f"Create {name} in the repository root. No other child "
                                                        "creates it.\n")),
                  (f"criteria {i}", lambda i=i, name=name: agent.write(
                      ws.temporary_dir / f"ac-{i}.md", f"- [ ] {name} exists in the repository root and is "
                                                       "committed\n")),
                  (f"new {i}", lambda i=i, name=name: new(i, name))]
    for i, name in enumerate(deliverables, 1):
        steps.append((f"plan {i}", lambda i=i, name=name: agent.ok(from_prompt(
            p, 'orch section set CHILD Plan -m "..."', CHILD=made[i - 1],
            **{"...": f"Write {name} and commit it on the child branch."}))))
    for i, _ in enumerate(deliverables, 1):
        steps.append((f"approve {i}", lambda i=i: agent.ok(from_prompt(p, "orch epic auto-approve CHILD",
                                                                       CHILD=made[i - 1]))))
    steps.append(("stop", lambda: STOP))
    return Script(agent, steps)


def deliverable(ws, child: str) -> str:
    """The file a child's criteria name (what the worker reads with `orch show`)."""
    return re.search(r"([\w-]+\.\w+)", store.load(ws, child)[1].section("Acceptance criteria")).group(1)


def worked_commit(prompt: str) -> str:
    """The worked `git commit` the clone prompt gives (`for example \\`git commit ...\\``)."""
    return next(s for s in snippets(prompt) if s.startswith("git commit") and "..." not in s)


def vfile(agent: Agent) -> Path:
    """Where the clone prompt says the Verification file goes: the workspace's temporary folder, by its full path."""
    return Path(agent.world.ws.temporary_dir).resolve() / f"{agent.child}-verification.md"


def worker(agent: Agent, name: str | None = None, body: str = "[]\n") -> Script:
    """Claim, read, one task, the file with the Write tool, git add and the worked commit as two commands, the task
    done, the Verification file with the Write tool and set from it, a last read, move to testing, stop."""
    p, c = agent.prompt, agent.child
    name = name or deliverable(agent.world.ws, c)
    evidence = f"- AC1: read {name} in the clone and saw it committed on the child branch\n"
    return Script(agent, [
        ("claim", lambda: agent.ok(from_prompt(p, f"orch claim {c}"))),
        ("show", lambda: agent.ok(from_prompt(p, f"orch show {c}"))),
        ("task", lambda: agent.ok(from_prompt(p, f'orch task add {c} "TASK"', TASK=f"Write {name}"))),
        ("start", lambda: agent.ok(from_prompt(p, f"orch task start {c} TN", TN="T1"))),
        ("write", lambda: agent.write(name, body)),
        ("add", lambda: agent.ok(from_prompt(p, "git add FILES", FILES=name))),
        ("commit", lambda: agent.ok(worked_commit(p))),
        ("done", lambda: agent.ok(from_prompt(p, f"orch task done {c} TN", TN="T1"))),
        ("evidence", lambda: agent.write(vfile(agent), evidence)),
        # the workspace path may hold a space ("my workspace" in the tests): quoted, as a model would
        ("verification", lambda: agent.ok(from_prompt(p, f"orch section set {c} Verification --file FILE",
                                                      FILE=f'"{vfile(agent)}"'))),
        ("check", lambda: agent.ok(from_prompt(p, f"orch show {c}"))),
        ("move", lambda: agent.bash(from_prompt(p, f"orch move {c} testing"))),
        ("stop", lambda: STOP)])


# -- the fake tmux ----------------------------------------------------------------------------------------------------

@dataclass
class Session:
    name: str
    cwd: str
    argv: list
    agent: Agent | None = None
    script: object = None
    waiting: bool = False  # the script ended its turn: idle at the prompt
    inbox: list = field(default_factory=list)  # nudges typed into the pane and sent with Enter
    screen: str | None = None  # a fixed screen instead of the script's (the trust question, ...)
    done: bool = False  # no steps left at all

    def pane(self) -> str:
        if self.screen is not None:
            return self.screen
        return IDLE if self.waiting and not self.inbox else BUSY


class Launcher:
    """The runner's Launcher (alive, start, stop) and what the nudge needs (capture, type, reap, human_typed), in
    memory. A started session gets its agent and script from the world."""

    def __init__(self, world):
        self.world = world
        self.sessions: dict[str, Session] = {}
        self.stopped: list[str] = []
        self.typed: list[tuple[str, str]] = []

    def alive(self):
        return {n for n in self.sessions if n not in self.stopped}

    def start(self, name, cwd, argv):
        s = self.sessions[name] = Session(name, cwd, argv)
        self.world.attach(s)
        return 4242  # conftest puts this pid in the process ancestry: the binding is trusted

    def stop(self, name):
        self.stopped.append(name)

    def reap(self, name):
        return None

    def capture(self, name):
        s = self.sessions.get(name)
        return None if s is None else s.pane()

    def human_typed(self, name):
        from orch.dashboard.factory_runner import human_typed
        return human_typed(name)

    def type(self, name, text):
        assert factory_runner.nudge_ok(text)  # the runner types only its own lines
        self.typed.append((name, text))
        self.sessions[name].inbox.append(text)
        return True


def binding_of(ws, name: str) -> dict | None:
    return next((b for b in fs.bindings(ws) if b["name"] == name), None)


# -- the world: the workspace, the human, the runner and the sessions ---------------------------------------------------

class World:
    """One workspace with its runner. `scripts` picks a session's script by child id, else "planner" or "worker" (a
    callable taking the Agent and returning a Script). `at(seconds)` moves the clock."""

    def __init__(self, ws, human, mp, remote: Path, state: Path, deliverables: list[str]):
        self.ws, self.human, self.mp, self.remote, self.state = ws, human, mp, remote, state
        self.deliverables = deliverables
        self.launcher = Launcher(self)
        self.scripts: dict = {"planner": lambda a: planner(a, self.deliverables), "worker": worker}
        self.lines: list[str] = []
        # the clock stands still unless a test moves it (a loaded machine must not age a pane into a nudge)
        self.offset, base = 0.0, clock.now()
        mp.setattr(clock, "now", lambda: base + timedelta(seconds=self.offset))
        self.epic: str | None = None
        # the CLI's click command tree, built once: typer rebuilds it on every call, which is most of a call's time
        import typer.main
        build, built = typer.main.get_command, {}
        mp.setattr(typer.main, "get_command", lambda app: built.get(id(app)) or built.setdefault(id(app), build(app)))

    def at(self, seconds: float) -> None:
        self.offset = max(self.offset, float(seconds))  # the clock never goes back

    # sessions
    def attach(self, s: Session) -> None:
        b = binding_of(self.ws, s.name)
        s.agent = Agent(self, b, s.argv[-1])  # {prompt} is the launch command's last word
        key = b["child"] if b["child"] in self.scripts else "planner" if fs.is_planner(b) else "worker"
        s.script = self.scripts[key](s.agent).run()

    def session(self, child: str) -> Session:
        """The latest session started for `child` (the epic for its planner)."""
        return [s for s in self.launcher.sessions.values() if s.agent.child == child][-1]

    def live(self) -> list[Session]:
        return [s for s in self.launcher.sessions.values() if s.name not in self.launcher.stopped]

    def step(self, s: Session) -> bool:
        """One step of session `s`; False when it has nothing to do: it waits at its prompt with nothing typed into
        it, has no steps left, shows a fixed screen, or the runner stopped it."""
        if s.done or s.name in self.launcher.stopped or s.screen is not None:
            return False
        try:
            if s.waiting:
                if not s.inbox:
                    return False
                s.waiting = False
                y = s.script.send(s.inbox.pop(0))
            else:
                y = next(s.script)
        except StopIteration:
            s.done = s.waiting = True
            return True
        s.waiting = y == STOP
        return True

    def run_sessions(self) -> bool:
        moved = False
        for s in self.live():
            while self.step(s):
                moved = True
        return moved

    # the dashboard's rounds
    def runner(self) -> list[str]:
        from orch.dashboard import launch
        out = factory_runner.tick(self.ws, self.human, self.launcher, settings=launch.load_settings())
        self.lines += out
        return out

    def release(self, run=None) -> list[str]:
        from orch.dashboard.factory_runner import release_once
        out = release_once(self.ws, run)
        self.lines += out
        return out

    def settle(self, rounds: int = 30, release: bool = True) -> None:
        """Runner rounds, each followed by every live session working until it waits, and a release round once the
        sessions wait, until nothing changes."""
        for _ in range(rounds):
            before = (len(self.launcher.sessions), len(self.launcher.stopped), len(self.lines))
            self.runner()
            if self.run_sessions():
                continue
            if release:
                self.release()
            if before == (len(self.launcher.sessions), len(self.launcher.stopped), len(self.lines)):
                return
        raise AssertionError(f"the run did not settle: {self.lines}")

    # the human
    def grant(self, rid: str, scope: str = "once") -> None:
        permits.permit_grant(self.ws, self.human, rid, scope, expected_sha=permits.requests(self.ws)[rid]["sha"])

    def deny(self, rid: str) -> None:
        permits.permit_deny(self.ws, self.human, rid, expected_sha=permits.requests(self.ws)[rid]["sha"])

    def cards(self) -> list[dict]:
        return permits.open_requests(self.ws)

    def requests(self) -> dict:
        return permits.requests(self.ws)
