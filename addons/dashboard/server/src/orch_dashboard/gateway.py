"""Host discovery. Only local trusted pins choose sockets; browser input never chooses paths."""

import json
import os
import re
import stat
import threading
from pathlib import Path

from . import adapters
from .adapters.addons import runner
from .click_signer import Signer
from .errors import fail
from .hostlink import HostLink
from .text import hidden

WORKSPACE_ID = re.compile(r"^(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$")


def state_dir(env=None) -> Path:
    """The state directory exactly as core resolves it (ORCH_STATE_DIR made absolute, an absolute
    XDG_CONFIG_HOME/orch, then HOME/.config/orch): the host and the click signer child read the same place."""
    from orch.ops.runtime import state_dir as core_state_dir

    return core_state_dir(os.environ if env is None else env)


class RunDirectoryError(RuntimeError):
    """A legacy override would send the dashboard to a different host."""


def run_dir(state: Path, env=None) -> Path:
    from orch.host.enable import run_dir_for

    env = os.environ if env is None else env
    run = run_dir_for(state, env=env)
    if "ORCH_RUN_DIR" in env and Path(env["ORCH_RUN_DIR"]).absolute() != run:
        raise RunDirectoryError(
            "ORCH_RUN_DIR is no longer read; set ORCH_HOST_RUN_DIR (the host's run directory) instead"
        )
    return run


def read_pins(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > 1_048_576:
            raise fail("peer.denied", exit=3)
        with os.fdopen(fd, "r", closefd=False) as f:
            data = json.load(f)
        # Provisional container shape, isolated pending stream A's host.list/registry schema.
        rows = data["workspaces"]
        if not isinstance(rows, list):
            raise ValueError("registry workspaces must be a list")
        pins = {}
        for row in rows:
            wid = row["workspace_id"]
            peer = row["peers"].get(str(os.getuid()), {})
            if not WORKSPACE_ID.fullmatch(wid) or row["host_uid"] != os.getuid():
                raise fail("peer.denied", exit=3)
            if peer.get("class") == "person" and peer.get("person"):
                pins[wid] = row
        return pins
    finally:
        os.close(fd)


# The inner ops of the settings hand-offs (M4). `grant` (issuing) is not here: the signer draws the secret.
WRITE_OPS = (
    "move",
    "settings.set",
    "policy.set",
    "member.add",
    "member.role",
    "member.remove",
    "grant.revoke",
    "addon.grant",
    "addon.disable",
)


class Gateway:
    def __init__(self, state=None, click=None):
        self.state = state or state_dir()
        self.run = run_dir(self.state)
        # The one signer child of this server (started on first use, so a dev server with no host starts none).
        self.click = click or Signer(self.state)
        self.links = {}
        self.control = None
        self.lock = threading.RLock()

    def _pins(self):
        try:
            return read_pins(self.state / "host/workspaces.json")
        except (OSError, ValueError, KeyError):
            raise fail("host.unavailable", "Host registry unavailable") from None

    def link(self, ws):
        with self.lock:
            pin = self._pins().get(ws)
            if not pin:
                raise fail("not_found", exit=2)
            existing = self.links.get(ws)
            if existing is None or existing.sock.fileno() < 0:
                # A dev host is just another socket. Its identity still has to match the registry pin.
                path = os.environ.get("ORCH_HOST_SOCKET") or self.run / ws / "host.sock"
                existing = HostLink.connect(path, pin["host_uid"], ws)
                self.links[ws] = existing
            if existing.welcome["peer"]["person"] != pin["peers"][str(os.getuid())]["person"]:
                existing.close()
                raise fail("peer.denied", exit=3)
            return existing

    def new_link(self, ws):
        """A host connection of its own (one per terminal attachment, section 12.6.6), pinned like `link`."""
        pin = self._pins().get(ws)
        if not pin:
            raise fail("not_found", exit=2)
        path = os.environ.get("ORCH_HOST_SOCKET") or self.run / ws / "host.sock"
        link = HostLink.connect(path, pin["host_uid"], ws)
        if link.welcome["peer"]["person"] != pin["peers"][str(os.getuid())]["person"]:
            link.close()
            raise fail("peer.denied", exit=3)
        return link

    def enabled(self, ws):
        link = self.link(ws)
        addons = link.call("addon.list")["addons"]
        if not any(a["name"] == "dashboard" and a["enabled"] is True for a in addons):
            raise fail("addon.disabled", exit=3)
        return link

    def features(self, ws):
        link = self.enabled(ws)
        features = []
        requirements = {
            "quick": [("quick.list", "quick/1")],
            # Worktrees (H11d, section 12.7): one git worktree per ticket and repo.
            "worktrees": [
                ("worktree.list", "worktree/1"),
                ("worktree.create", "worktree/1"),
                ("worktree.remove", "worktree/1"),
            ],
            "records": [("records.status", "records/1")],
            "identity": [
                ("whoami", "ops/1"),
                ("member.list", "query/1"),
                ("workspace.show", "query/1"),
                ("host.status", "ops/1"),
            ],
            "today": [("needs", "query/1"), ("ticket.query", "query/1")],
            "tickets": [("ticket.query", "query/1"), ("needs", "query/1")],
            "board": [("ticket.query", "query/1"), ("needs", "query/1")],
            "ticket": [("ticket.get", "query/1"), ("needs", "query/1")],
            "changes": [("ticket.get", "query/1"), ("diff", "diff/1")],
            "evidence": [("ticket.get", "query/1"), ("artifact.get", "diff/1")],
            "activity": [("activity.list", "activity/1")],
            # The signed actions (M3): prepare, look at and cancel a hand-off. The dashboard never submits.
            "actions": [
                ("human.prepare", "human/1"),
                ("human.get", "human/1"),
                ("human.pending", "human/1"),
                ("human.cancel", "human/1"),
                ("ticket.get", "query/1"),
            ],
            # Reduced workspace settings (M4). `settings` is the page: the roster is `query/1`, so it is there on
            # every host that serves reads; each section then says what this host cannot show. `settings_read` is
            # `workspace.settings` (cap `settings/1`): without it durations, policies and repos are unknown.
            "settings": [("member.list", "query/1"), ("workspace.show", "query/1")],
            "members": [("member.list", "query/1")],
            "settings_read": [("workspace.settings", "settings/1")],
            "grant_list": [("grant.list", "query/1")],
            # H12d-2: the readers' levels (Settings → Devices) and `required_reader`.
            "readers": [("reader.list", "reader/1")],
            # Terminals (M7a, section 12.6): the person's own shells.
            "terminals": [
                ("run.sub", "run/1"),
                ("run.list", "run/1"),
                ("run.stop", "run/1"),
                ("term.preview", "term/1"),
                ("term.open", "term/1"),
                ("term.attach", "term/1"),
                ("attach.cancel", "term/1"),
            ],
            # Start agent (M7b, H11c): preview and start an agent run; the person's runner grant (read through
            # `whoami.runner`, forgotten with `grant.forget`, a deposit's outcome read with `grant.deposit_status`).
            # Issuing the grant is a click request of the signer child (`click.py`), never a host op of this process.
            "agent_start": [
                ("agent.preview", "run/1"),
                ("agent.start", "run/1"),
                ("run.list", "run/1"),
                ("whoami", "ops/1"),
            ],
            "runner_grant": [
                ("grant.deposit_status", "run/1"),
                ("grant.forget", "run/1"),
                ("whoami", "ops/1"),
            ],
        }
        for feature, ops in requirements.items():
            if all(link.supports(op, cap) for op, cap in ops):
                features.append(feature)
        if link.has("read_ext/1"):
            features.append("read_ext")  # the optional v1.4 fields are sent: an absent one is unknown, not zero
        if link.has("read_ext/2"):
            features.append("read_ext2")  # grant revoked{by,at,seq}, session_ids: absent is unknown
        if "actions" in features:
            # The member, grant and settings writes go through `human.prepare` as the inner op. The host lists every
            # op it serves in `welcome.ops`; one it does not list (policy.set and settings.set wait for F1 events,
            # R5) is not offered. Hiding a control is a courtesy: the host stays the authority.
            served = link.welcome.get("ops", {})
            features += [f"do:{op}" for op in WRITE_OPS if served.get(op) == 1]
            if served.get("move") == 1:
                features.append("move")  # the board's move action (H6b): backlog <-> open through a hand-off
        return features

    def one_path_is_click(self, ws) -> bool:
        """Is click-to-sign switched ON for this workspace (the signer's flag)? Then it is the ONE signing path: the
        dashboard prepares no terminal hand-off there (owner decision, 12 Oct 2026)."""
        card = self.click.status().get(ws)
        return isinstance(card, dict) and card.get("enabled") is True

    def workspaces(self):
        with self.lock:
            pins = self._pins()
            if self.control is None or self.control.sock.fileno() < 0:
                self.control = HostLink.connect(self.run / "control/host.sock", os.getuid(), None)
            rows = self.control.call("host.list")["workspaces"]
        result = []
        from .errors import HostError

        signer = self.click.status()  # custody, enabled, available, problem per workspace; empty without a signer
        for row in rows:
            ws = row["workspace_id"]
            if ws not in pins or row.get("person") != pins[ws]["peers"][str(os.getuid())]["person"]:
                continue
            try:
                link = self.enabled(ws)
                show = link.call("workspace.show")
                roster = adapters.members(link.call("member.list"))
                features = self.features(ws)
                card = signer.get(ws)
                if card and "actions" in features and card.get("enabled") and card.get("available"):
                    features.append("click")  # the sign panel is offered (the signer has a key and the flag is on)
                who = link.welcome["peer"]["person"]
                role = next((m.role for m in roster if m.person == who), "viewer")
                count = adapters.actionable_count(link.call("needs")["items"], role) if "today" in features else None
                if (
                    not isinstance(show.get("name"), str)
                    or not isinstance(show.get("prefix"), str)
                    or hidden(show["name"])
                    or hidden(show["prefix"])
                ):
                    # Not repaired, not shown: invisible or bidi characters could make a name pass for another.
                    raise fail("workspace.name_invalid", "The workspace name has invisible characters.", exit=3)
                result.append(
                    adapters.Workspace(
                        id=ws,
                        prefix=show["prefix"],
                        name=show["name"],
                        members=roster,
                        needs_you=count,
                        features=features,
                        click=adapters.click_card(card),
                        runner=runner(link.call("whoami"), True)
                        if link.supports("whoami", "ops/1") and link.has("run/1")
                        else None,
                    )
                )
            except HostError as exc:
                # No untrusted title or principal is fabricated when the host is unavailable.
                result.append(adapters.Workspace(id=ws, prefix="", name=ws, host_state=exc.error["code"]))
        return result

    def host_info(self):
        """The host-level dev facts (core D1, #582) from the last `host.list`, checked: `dev` and `alarms`, each absent
        when the host sends none. Never more than the closed shape below reaches the page."""
        with self.lock:
            if self.control is None or self.control.sock.fileno() < 0:
                self.control = HostLink.connect(self.run / "control/host.sock", os.getuid(), None)
            return adapters.host_info(self.control.call("host.list"))

    def call(self, ws, op, args=None):
        return self.enabled(ws).call(op, args)

    def close(self):
        self.click.close()
        for link in [self.control, *self.links.values()]:
            if link:
                link.close()
