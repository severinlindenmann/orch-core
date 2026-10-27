"""``orch host prune``: the inventory, the removal plan and its fences, on a fake machine in a temp folder.

No test touches launchctl, systemctl, uv or a keychain: every outside effect is a seam on ``Ctx``."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from orch.instructions import harness
from orch.prune import execute as X
from orch.prune.cli import main
from orch.prune.inventory import edit_text, gather
from orch.prune.model import Ctx, Fence, PruneError

WID = "ab" * 16
USER_AGENTS = "# My project rules\n\nBe kind.\n"


class FakeRunner:
    kind = "fake"

    def __init__(self, fail: str | None = None):
        self.stopped: list[str] = []
        self.fail = fail

    def stop(self, part):
        if part == self.fail:
            raise RuntimeError("launchctl said no")
        self.stopped.append(part)

    def info(self, part):
        return {"running": True, "pid": 1, "service": f"fake.{part}"}


class Machine:
    def __init__(self, tmp_path: Path, platform: str = "darwin"):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True)
        self.state = self.home / ".config" / "orch"
        self.ws = self.home / "work" / "proj"
        self.platform = platform
        self.calls: list[list[str]] = []
        self.runner = FakeRunner()
        self.answers: list[str] = []
        self.asked: list[str] = []
        self.keychain_deleted: list[str] = []
        self.accounts: list[tuple[str, str]] = []
        self._build()

    def w(self, path: Path, text: str = "x", mode: int | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mode is not None:
            path.chmod(mode)
        return path

    def _build(self) -> None:
        s, h = self.state, self.home
        self.w(
            s / "install" / "install.json",
            json.dumps(
                {
                    "v": 1,
                    "address": None,
                    "domain": None,
                    "parts": {"hub": {"url": "http://127.0.0.1:8100", "service": "fake.hub"}},
                    "dashboard": {"url": "http://127.0.0.1:8151/", "service": "fake.dashboard"},
                    "workspaces": [{"dir": str(self.ws), "port": 8151}],
                }
            ),
        )
        self.w(s / "install" / "parts" / "hub" / "data" / "keys.json", "hub-secret")
        self.w(s / "install" / "tools" / "t.bin")
        self.w(s / "install" / "logs" / "hub.log")
        self.w(
            s / "host" / "workspaces.json",
            json.dumps(
                {
                    "v": 1,
                    "workspaces": [
                        {
                            "workspace_id": WID,
                            "prefix": "PW",
                            "root": str(self.ws),
                            "host_uid": 501,
                            "group": "orch-" + WID[:12],
                            "peers": {},
                            "repos": {},
                        }
                    ],
                }
            ),
        )
        self.w(s / "hosts" / WID / "keys" / "wsk.filekey.json", "{}")
        self.w(s / "hosts" / WID / "hub" / "state.json")
        self.w(s / "hosts" / "hook-secrets.json")
        self.w(s / "person" / WID / "custody.json")
        self.w(s / "dashboard" / "passkeys.json")
        # a link inside the state folder that points outside: must be left alone, never followed
        self.outside = self.root / "outside"
        self.w(self.outside / "precious.txt", "keep me")
        os.symlink(self.outside, s / "evil-link")
        rel = h / ".local" / "share" / "orch" / "releases"
        self.w(rel / "v1" / "bin" / "orch-host", "#!", 0o444)
        (rel / "v1" / "bin").chmod(0o555)
        (rel / "v1").chmod(0o555)
        if self.platform == "darwin":
            for name in ("io.orch.host", "io.orch.runner", "io.orch.records"):
                self.w(h / "Library" / "LaunchAgents" / f"{name}.plist", "<plist/>")
        else:
            for name in ("orch-host.service", "orch-runner.service", "orch-records.timer"):
                self.w(h / ".config" / "systemd" / "user" / name, "[Unit]")
        # the workspace
        (self.ws / ".orch").mkdir(parents=True)
        self.w(
            self.ws / ".orch" / "config.json",
            json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w_" + WID}}),
        )
        self.w(self.ws / ".orch" / "tickets" / "T1.md", "ticket")
        self.w(self.ws / ".orch-work" / "PW-0001" / "out.txt", "work")
        self.w(self.ws / "AGENTS.md", USER_AGENTS)
        self.w(self.ws / ".gitignore", "node_modules/\n")
        self.w(self.ws / "notes.txt", "my own notes")
        self.w(self.ws / "src" / "main.py", "print(1)")
        harness.write_workspace_files(self.ws, pointers=True, store=self.ws / ".orch")
        # worktree registered in the pins
        self.wt = Path(str(self.ws) + ".worktrees") / (WID[:8] + "-PW-0001")
        self.w(self.wt / "f.txt")
        pins = {
            "v": 1,
            "repos": {},
            "worktrees": {"u1": {"repo": {"gen": 1, "reg": {"path": str(self.wt), "branch": "b"}}}},
        }
        self.w(s / "hosts" / WID / "git-pins.json", json.dumps(pins))
        # Claude Code's file: orch's trust entries plus a project of the person's
        trust = {k: True for k in X.TRUST_KEYS}
        claude = {
            "theme": "dark",
            "projects": {
                str(self.ws / ".orch-work" / "PW-0001"): {**trust},
                str(self.home / "other"): {"hasTrustDialogAccepted": True},
                str(self.ws / ".orch-work" / "PW-0002"): {**trust, "allowedTools": ["x"]},
            },
        }
        self.w(h / ".claude.json", json.dumps(claude))

    # -- seams
    def run(self, argv, env=None):
        self.calls.append(list(argv))
        if argv[:3] == ["uv", "tool", "list"]:
            return 0, "orch-core v2.0.0\n- orch\n"
        return 0, ""

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def keychain(self, path):
        m = self

        class H:
            def accounts(self):
                return [f"{WID}/d_{'1' * 32}", "something-else"]

            def delete(self, account):
                m.keychain_deleted.append(account)

        return H()

    def ctx(self, **kw) -> Ctx:
        self.out, self.err = io.StringIO(), io.StringIO()
        kw.setdefault("interactive", False)
        return Ctx(
            env={"HOME": str(self.home), "PATH": "/usr/bin"},
            out=self.out,
            err=self.err,
            platform=self.platform,
            run=self.run,
            ask=self.ask,
            runner=self.runner,
            os_accounts=lambda: self.accounts,
            keychain=self.keychain,
            dashboard_entry=lambda: ["/bin/orch-dashboard"],
            euid=1000,
            uid=501,
            root_exists=lambda p: False,
            hub_leave=lambda root, env: (
                (self.left.append(root) or True, "ok") if hasattr(self, "left") else (True, "ok")
            ),
            **kw,
        )

    def prune(self, *args: str, env_extra: dict | None = None, **kw) -> int:
        ctx = self.ctx(**kw)
        env = {**ctx.env, **(env_extra or {})}
        ctx.env = env
        return main(list(args), env=env, out=self.out, err=self.err, ctx=ctx)

    def snapshot(self) -> list[str]:
        out = []
        for base in (self.home, self.outside):
            for dp, dn, fn in os.walk(base):
                out += [os.path.join(dp, n) for n in dn + fn]
        return sorted(out)


@pytest.fixture
def m(tmp_path):
    mach = Machine(tmp_path)
    yield mach
    for dp, dn, _ in os.walk(mach.root):  # the read-only release copies must not block pytest's cleanup
        for d in dn:
            try:
                os.chmod(os.path.join(dp, d), 0o755)
            except OSError:
                pass


# -------------------------------------------------------------------------------------------------- inventory


def test_inventory_lists_everything_grouped(m):
    inv = gather(m.ctx())
    paths = {i.path for i in inv.items}
    assert str(m.home / "Library/LaunchAgents/io.orch.host.plist") in paths
    assert str(m.home / "Library/LaunchAgents/io.orch.runner.plist") in paths
    assert str(m.home / "Library/LaunchAgents/io.orch.records.plist") in paths
    assert {i.data["part"] for i in inv.items if i.kind == "part"} == {"hub", "dashboard"}
    assert str(m.state / "install" / "parts" / "hub") in paths  # a part's data is its own line (its keys live there)
    assert str(m.state / "host") in paths and str(m.state / "hosts" / WID) in paths and str(m.state / "person") in paths
    assert str(m.home / ".local/share/orch/releases") in paths
    assert any(i.kind == "note" and i.group == "Keys" for i in inv.items)
    assert any(i.kind == "claude-trust" and len(i.data["keys"]) == 2 for i in inv.items)  # only the work folders
    assert any(i.kind == "tool" for i in inv.items)
    assert str(m.state / "evil-link") not in paths
    assert any("evil-link" in w for w in inv.warnings)
    [ws] = inv.workspaces
    assert ws.root == m.ws and ws.wid == WID and not ws.folder_refusal and ws.hub
    wk = {(i.kind, i.path) for i in ws.items}
    assert ("tree", str(m.ws / ".orch")) in wk and ("tree", str(m.ws / ".orch-work")) in wk
    assert ("worktree", str(m.wt)) in wk
    assert {i.data["rel"] for i in ws.items if i.kind == "skill"} == {
        f"{b}/{n}"
        for b in (".agents/skills", ".claude/skills")
        for n in ("orch-tickets", "orch-work-on-ticket", "orch-refine-ticket")
    }
    assert {i.data["rel"] for i in ws.items if i.kind == "edit"} == {"AGENTS.md", ".gitignore"}
    assert any("hub membership" in r for r in inv.remote)
    assert any("browser data" in r for r in inv.not_removable)


def test_dry_run_prints_and_removes_nothing(m):
    before = m.snapshot()
    assert m.prune("--dry-run") == 0
    assert m.snapshot() == before
    assert not m.runner.stopped
    assert "--dry-run: nothing was removed" in m.out.getvalue()
    assert str(m.state / "hosts" / WID) in m.out.getvalue()
    assert not [c for c in m.calls if c[0] in ("launchctl", "git")]


def test_system_files_listed_with_commands_for_an_admin(m):
    m.platform = "linux"
    m.accounts = [("orch-agent-ann", "user"), ("orch-addon-1", "user"), ("orch-agent-ann", "group")]
    ctx = m.ctx()
    ctx.platform, ctx.root_exists = "linux", lambda p: p in ("/etc/orch", "/var/lib/orch")
    inv = gather(ctx)
    assert "systemctl disable --now orch-runner@ann.service" in inv.root_commands
    assert "userdel orch-agent-ann" in inv.root_commands and "groupdel orch-" + WID[:12] in inv.root_commands
    assert "rm -rf /etc/orch" in inv.root_commands and "rm -rf /var/lib/orch" in inv.root_commands


# ------------------------------------------------------------------------------------------------------- removal


def test_yes_keep_removes_orch_and_leaves_the_users_files(m):
    assert m.prune("--yes", "--workspaces", "keep") == 0, m.err.getvalue() + m.out.getvalue()
    assert os.listdir(m.state) == ["evil-link"]  # only the link out of the folder is left, with a note, not followed
    assert "evil-link" in m.out.getvalue()
    assert not (m.home / ".config" / "systemd").exists() and not (m.home / ".local/share/orch").exists()
    assert not list((m.home / "Library" / "LaunchAgents").iterdir())
    assert m.runner.stopped == ["hub", "dashboard"]
    assert ["launchctl", "bootout", "gui/501/io.orch.host"] in m.calls
    assert ["uv", "tool", "uninstall", "orch-core"] == m.calls[-1]  # the tool itself is the last step
    ws = m.ws
    assert not (ws / ".orch").exists() and not (ws / ".orch-work").exists()
    assert not (ws / ".agents").exists() and not (ws / ".claude").exists()
    assert (ws / "AGENTS.md").read_text() == USER_AGENTS
    assert (ws / ".gitignore").read_text() == "node_modules/\n"
    assert (ws / "CLAUDE.md").read_text() == "@AGENTS.md\n"  # "@AGENTS.md" is not orch's line while AGENTS.md is yours
    assert (ws / "notes.txt").read_text() == "my own notes" and (ws / "src" / "main.py").exists()
    assert ["git", "-C", str(ws), "worktree", "remove", "--force", str(m.wt)] in m.calls
    claude = json.loads((m.home / ".claude.json").read_text())
    assert claude["theme"] == "dark"
    assert set(claude["projects"]) == {str(m.home / "other"), str(ws / ".orch-work" / "PW-0002")}
    assert claude["projects"][str(ws / ".orch-work" / "PW-0002")] == {"allowedTools": ["x"]}  # Claude's own data stays
    assert (m.outside / "precious.txt").read_text() == "keep me"  # the link was not followed


def test_after_prune_a_second_run_finds_nothing(m):
    assert m.prune("--yes", "--workspaces", "keep") == 0
    m.runner.stopped.clear()
    assert m.prune("--yes", "--workspaces", "keep", "--keep-tool") == 0
    assert "Nothing to remove" in m.out.getvalue() or "Done" in m.out.getvalue()


def test_yes_alone_never_deletes_a_workspace_folder(m):
    assert m.prune("--yes") == 0
    assert m.ws.exists() and (m.ws / "notes.txt").exists() and (m.ws / "src" / "main.py").exists()


def test_workspaces_delete_removes_the_folder_after_its_worktrees(m):
    assert m.prune("--yes", "--workspaces", "delete") == 0, m.err.getvalue()
    assert not m.ws.exists()
    assert ["git", "-C", str(m.ws), "worktree", "remove", "--force", str(m.wt)] in m.calls
    assert (m.outside / "precious.txt").exists()


def test_a_folder_prune_does_not_recognise_is_never_deleted(m):
    (m.ws / ".orch" / "config.json").unlink()
    assert m.prune("--yes", "--workspaces", "delete") == 0
    assert m.ws.exists() and (m.ws / "notes.txt").exists()
    assert "no orch store" in m.out.getvalue()


def test_interactive_confirm_and_per_workspace_question(m):
    m.answers = ["y", "n", "n"]  # remove all? yes; delete the folder? no; tell the hub? no
    assert m.prune(interactive=True) == 0, m.err.getvalue()
    assert any("Remove all of this?" in a for a in m.asked)
    assert any(f"Also delete the folder {m.ws} (" in a and a.endswith("[y/N] ") for a in m.asked)
    assert m.ws.exists() and not (m.ws / ".orch").exists()


def test_interactive_yes_to_the_folder_deletes_it(tmp_path):
    m = Machine(tmp_path)
    m.answers = ["y", "y", "n"]
    assert m.prune(interactive=True) == 0
    assert not m.ws.exists()


def test_decline_removes_nothing(m):
    before = m.snapshot()
    m.answers = ["n"]
    assert m.prune(interactive=True) == 5
    assert m.snapshot() == before and not m.runner.stopped


def test_hub_leave_is_offered_only_with_a_person_at_the_terminal(tmp_path):
    m = Machine(tmp_path)
    m.left = []
    m.answers = ["y", "n", "y"]  # remove; keep folder; tell the hub
    assert m.prune(interactive=True) == 0
    assert m.left == [m.ws]
    assert not any("hub membership" in ln and "has a hub" in ln for ln in m.out.getvalue().split("Left on other")[-1:])
    m2 = Machine(tmp_path / "b")
    m2.left = []
    assert m2.prune("--yes", "--workspaces", "keep") == 0
    assert m2.left == []
    assert "Left on other computers" in m2.out.getvalue() and "hub membership" in m2.out.getvalue()


def test_no_terminal_and_no_yes_is_refused(m):
    before = m.snapshot()
    assert m.prune() == 2
    assert m.snapshot() == before
    assert "--yes" in m.err.getvalue()


def test_agents_are_refused(m):
    before = m.snapshot()
    assert m.prune("--yes", env_extra={"ORCH_SESSION": "s1"}) == 3
    assert m.prune("--yes", env_extra={"ORCH_GRANT": "g"}) == 3
    assert m.snapshot() == before


@pytest.mark.parametrize("var", ["ORCH_DEV", "ORCH_DEV_KIT", "ORCH_KEYCHAIN"])
def test_dev_kit_is_refused_unless_told(m, var):
    before = m.snapshot()
    assert m.prune("--yes", env_extra={var: "1"}) == 4
    assert "dev kit" in m.err.getvalue() and m.snapshot() == before
    assert m.prune("--dry-run", env_extra={var: "1"}) == 0  # looking is harmless
    assert m.prune("--yes", "--workspaces", "keep", "--i-know-dev", env_extra={var: "1"}) == 0


def test_dev_sandbox_marker_in_the_state_dir_is_refused(m):
    m.w(m.state / "host" / "dev.json", "{}")
    assert m.prune("--yes") == 4


def test_stops_at_the_first_error_and_says_what_is_left(m):
    m.runner.fail = "hub"
    assert m.prune("--yes", "--workspaces", "keep") == 1
    text = m.out.getvalue()
    assert "orch host prune stopped" in text and "could not stop the hub service" in text
    assert m.state.exists() and (m.ws / ".orch").exists()  # nothing after the failed step ran
    assert "Still on this computer" in text and str(m.state / "host") in text


def test_keychain_items_of_orch_only(m):
    assert m.prune("--yes", "--workspaces", "keep", env_extra={"ORCH_KEYCHAIN": ""}) == 0
    assert m.keychain_deleted == []  # no keychain file on this fake machine
    kc = m.home / "Library" / "Keychains" / "login.keychain-db"
    m.w(kc)
    m2 = m
    m2.state.mkdir(parents=True, exist_ok=True)
    assert m2.prune("--yes", "--workspaces", "keep") == 0
    assert m.keychain_deleted == [f"{WID}/d_{'1' * 32}"]  # "something-else" is not ours


def test_root_commands_are_printed_for_a_normal_user_and_run_as_root(m, monkeypatch):
    m.platform = "linux"
    m.accounts = [("orch-agent-ann", "user")]
    removed = []
    monkeypatch.setattr(X, "rm_tree", lambda p: removed.append(os.fspath(p)))

    def run(root: bool):
        ctx = m.ctx()
        ctx.platform, ctx.root_exists, ctx.euid = "linux", (lambda p: p == "/etc/orch"), 0 if root else 1000
        inv = gather(ctx)
        steps = [i for i in X.build_steps(inv, set()) if i.kind == "os-batch"]
        return X.execute(ctx, inv, steps)

    res = run(False)
    assert "sudo userdel orch-agent-ann" in res.admin and "sudo rm -rf /etc/orch" in res.admin
    res = run(True)
    assert not res.admin and "/etc/orch" in removed
    assert ["/usr/sbin/userdel", "orch-agent-ann"] in m.calls


def test_windows_prints_the_uninstall_command_instead_of_running_it(m):
    m.platform = "win32"
    ctx = m.ctx()
    inv = gather(ctx)
    assert not any(i.kind == "unit" for i in inv.items)
    res = X.execute(ctx, inv, [i for i in X.build_steps(inv, set()) if i.kind == "tool"])
    assert any("uv tool uninstall orch-core" in c for c in res.admin)
    assert ["uv", "tool", "uninstall", "orch-core"] not in m.calls


def test_systemd_units_are_stopped_with_systemctl(tmp_path):
    m = Machine(tmp_path, platform="linux")
    assert m.prune("--yes", "--workspaces", "keep") == 0
    assert ["systemctl", "--user", "disable", "--now", "orch-host.service"] in m.calls
    assert not (m.home / ".config" / "systemd" / "user" / "orch-host.service").exists()


# ------------------------------------------------------------------------------------------------------- fences


def test_fence_refuses_outside_home_shallow_and_linked_paths(tmp_path):
    home = tmp_path / "home"
    root = home / ".config" / "orch"
    (root / "a").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", root / "a" / "lnk")
    f = Fence([root], home)
    assert f.check(root / "a")
    for bad in (tmp_path / "elsewhere", home, home.parent, Path("/"), Path("/etc"), root / "a" / "lnk" / "x"):
        with pytest.raises(PruneError):
            f.check(bad)
    assert f.check(root / "a" / "lnk") == root / "a" / "lnk"  # the link itself may go; nothing behind it


def test_fence_refuses_a_state_dir_that_is_the_home(tmp_path):
    with pytest.raises(PruneError):
        Fence([tmp_path], tmp_path).check(tmp_path)


def test_a_linked_workspace_folder_is_left_alone(m, tmp_path):
    real = tmp_path / "realws"
    real.mkdir()
    (real / "keep.txt").write_text("k")
    link = m.home / "linkws"
    os.symlink(real, link)
    inv = gather(m.ctx(), [str(link)])
    assert all(w.root != link for w in inv.workspaces)
    assert any("linkws" in w for w in inv.warnings)


def test_a_custom_state_dir_keeps_foreign_entries(m, tmp_path):
    custom = m.home / "mystate"
    m.state.rename(custom)
    m.w(custom / "not-orchs.txt", "x")
    ctx = m.ctx()
    ctx.env = {**ctx.env, "ORCH_STATE_DIR": str(custom)}
    inv = gather(ctx)
    assert str(custom / "not-orchs.txt") not in {i.path for i in inv.items}
    assert any("not-orchs.txt" in w for w in inv.warnings)


def test_release_copies_are_read_only_and_still_go(m):
    rel = m.home / ".local/share/orch/releases/v1/bin/orch-host"
    assert not os.access(rel.parent, os.W_OK)
    X.rm_tree(m.home / ".local/share/orch/releases")
    assert not (m.home / ".local/share/orch/releases").exists()
    assert stat.S_ISREG(os.stat(m.ws / "notes.txt").st_mode)


# --------------------------------------------------------------------------------------------- text edits


def test_edit_text_removes_only_orchs_lines():
    block = harness.AGENTS_BLOCK_STORE
    assert edit_text("agents-block", USER_AGENTS + "\n" + block + "\n") == "# My project rules\n\nBe kind.\n"
    assert edit_text("agents-block", block + "\n") == ""
    assert edit_text("agents-block", USER_AGENTS) is None
    assert edit_text("claude-import", "@AGENTS.md\n@AGENTS.orch.md\n") == "@AGENTS.md\n"
    assert edit_text("claude-import", "@AGENTS.md\n", agents_gone=True) == ""  # orch's own file, its target is gone
    assert edit_text("claude-import", "@AGENTS.md\n") is None  # AGENTS.md is still yours: untouched
    assert edit_text("claude-import", "# mine\n@AGENTS.md\n", agents_gone=True) is None
    assert edit_text("claude-import", "@AGENTS.orch.md\n@AGENTS.md\n", agents_gone=True) == ""
    assert edit_text("gitignore", "dist/\n.orch/\n.orch-work/\n") == "dist/\n"
    assert edit_text("gitignore", ".orch/\n") == ""


def test_a_hand_edited_orch_skill_is_kept(m):
    (m.ws / ".agents/skills/orch-tickets/SKILL.md").write_text("---\nname: orch-tickets\n---\nmine\n")
    sidecar = m.ws / ".agents/skills/orch-tickets/orch.skill.json"
    side = json.loads(sidecar.read_text())
    assert side["scope"] == "builtin"
    assert m.prune("--yes", "--workspaces", "keep") == 0
    assert (m.ws / ".agents/skills/orch-tickets/SKILL.md").read_text().endswith("mine\n")


def test_files_orch_created_alone_are_deleted(tmp_path):
    m = Machine(tmp_path)
    (m.ws / "AGENTS.md").unlink()
    harness.write_workspace_files(m.ws, pointers=True, store=m.ws / ".orch")  # AGENTS.md and CLAUDE.md are orch's now
    assert (m.ws / "CLAUDE.md").exists() and harness.BEGIN in (m.ws / "AGENTS.md").read_text()
    assert m.prune("--yes", "--workspaces", "keep") == 0, m.err.getvalue()
    assert not (m.ws / "AGENTS.md").exists() and not (m.ws / "CLAUDE.md").exists()
    assert (m.ws / "notes.txt").exists()


def test_a_unit_loaded_from_another_file_is_left_running(m):
    def run(argv, env=None):
        m.calls.append(list(argv))
        if argv[:3] == ["launchctl", "print", "gui/501/io.orch.host"]:
            return 0, "io.orch.host = {\n\tpath = /Users/other/Library/LaunchAgents/io.orch.host.plist\n}\n"
        if argv[:3] == ["uv", "tool", "list"]:
            return 0, "orch-core v2\n"
        return 0, ""

    m.run = run
    assert m.prune("--yes", "--workspaces", "keep") == 0
    assert ["launchctl", "bootout", "gui/501/io.orch.host"] not in m.calls  # the other install keeps running
    assert ["launchctl", "bootout", "gui/501/io.orch.runner"] in m.calls
    assert "left running" in m.out.getvalue()
    assert not (m.home / "Library/LaunchAgents/io.orch.host.plist").exists()  # our own file is still removed


def test_a_host_started_by_hand_is_reported(m, monkeypatch):
    import socket

    from orch.prune import inventory

    sock_dir = Path("/tmp") / f"hk{os.getpid()}"  # short: a unix socket path holds ~100 bytes
    sock_dir.mkdir(exist_ok=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(sock_dir / "h.sock"))
    srv.listen(1)
    try:
        assert inventory._answers(sock_dir / "h.sock")
        assert not inventory._answers(sock_dir / "nobody.sock")
    finally:
        srv.close()
        os.unlink(sock_dir / "h.sock")
        sock_dir.rmdir()
    monkeypatch.setattr(inventory, "_answers", lambda p: True)
    assert any("not a service" in w for w in gather(m.ctx()).warnings)


def test_a_part_is_stopped_by_the_runner_its_record_names(tmp_path):
    from orch.install import service

    m = Machine(tmp_path)
    rec = json.loads((m.state / "install" / "install.json").read_text())
    rec["parts"]["hub"]["runner"] = "pidfile"
    (m.state / "install" / "install.json").write_text(json.dumps(rec))
    ctx = m.ctx()
    ctx.runner = None
    from orch.install.state import Layout
    from orch.prune.inventory import install_runner

    assert isinstance(install_runner(ctx, Layout(ctx.env), "pidfile"), service.PidfileRunner)
    assert isinstance(install_runner(ctx, Layout(ctx.env), "launchd"), service.LaunchdRunner)


def test_the_claude_code_plugin_entries_go_through_claude_itself(m):
    plug = m.home / ".claude" / "plugins"
    m.w(
        plug / "installed_plugins.json",
        json.dumps({"version": 2, "plugins": {"orch-core@orch-core": [{}], "x@y": [{}]}}),
    )
    m.w(plug / "known_marketplaces.json", json.dumps({"orch-core": {}, "other": {}}))
    assert m.prune("--yes", "--workspaces", "keep") == 0
    assert ["claude", "plugin", "uninstall", "orch-core@orch-core"] in m.calls
    assert ["claude", "plugin", "marketplace", "remove", "orch-core"] in m.calls
    assert (plug / "installed_plugins.json").exists()  # Claude's files are never edited by prune


def test_hub_leave_never_imports_a_file_of_the_workspace(m, tmp_path):
    from orch.prune.model import _leave

    marker = tmp_path / "hijacked"
    (m.ws / "orch.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    (m.ws / "orch").mkdir()
    (m.ws / "orch" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('x')\n")
    env = {**os.environ, "HOME": str(m.home), "PYTHONPATH": str(m.ws)}
    _leave(m.ws, env)
    assert not marker.exists()


def test_the_dashboard_lookup_always_sanitizes_pythonpath(monkeypatch):
    from orch.cli import dashboard

    seen = {}
    monkeypatch.setattr(dashboard, "_module_installed", lambda env: seen.update(env) or False)
    monkeypatch.setattr(dashboard.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.Path, "is_file", lambda self: False)
    dashboard.find_entry_point({"PATH": "/usr/bin", "PYTHONPATH": ":rel:/abs/ok"})
    assert seen["PYTHONPATH"] == "/abs/ok"
    assert dashboard.launch_env({"PYTHONPATH": ":rel"}) == {}


def test_the_dashboard_stop_runs_with_the_sanitized_environment(m):
    seen = []
    m.run = lambda argv, env=None: seen.append((argv, env)) or (0, "")
    m.w(m.state / "dashboard" / "ctl.sock")
    ctx = m.ctx()
    ctx.env = {**ctx.env, "PYTHONPATH": ":rel:/abs"}
    ctx.dashboard_entry = lambda: ["/bin/orch-dashboard"]
    inv = gather(ctx)
    X.execute(ctx, inv, [i for i in X.build_steps(inv, set()) if i.kind == "launcher"])
    [(argv, env)] = [x for x in seen if x[0][-1] == "stop"]
    assert env["PYTHONPATH"] == "/abs"
