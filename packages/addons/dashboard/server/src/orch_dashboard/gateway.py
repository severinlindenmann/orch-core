"""Host discovery. Only local trusted pins choose sockets; browser input never chooses paths."""

import copy
import json
import os
import re
import stat
import threading
from pathlib import Path

from . import adapters
from .adapters.addons import runner
from .click_signer import Signer
from .errors import HostError, fail
from .hostlink import REQUEST_MEMO, HostLink
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


def read_rows(path):
    """The registry's rows, from a file this user owns and no one else can read or write."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > 1_048_576:
            raise fail("peer.denied", exit=3)
        with os.fdopen(fd, "r", closefd=False) as f:
            data = json.load(f)
        # Provisional container shape, isolated pending stream A's host.list/registry schema.
        rows = data["workspaces"] if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("registry workspaces must be a list")
        return rows
    finally:
        os.close(fd)


def read_pins(path):
    pins = {}
    for row in read_rows(path):
        wid = row["workspace_id"]
        peer = row["peers"].get(str(os.getuid()), {})
        if not WORKSPACE_ID.fullmatch(wid) or row["host_uid"] != os.getuid():
            raise fail("peer.denied", exit=3)
        if peer.get("class") == "person" and peer.get("person"):
            pins[wid] = row
    return pins


def read_people(path):
    """``{workspace id: person}`` of this computer's user, like `read_pins` but forgiving: a malformed row (missing or
    mistyped id, host uid, peers or person) is SKIPPED, never trusted and never an error for the others."""
    uid = os.getuid()
    people = {}
    for row in read_rows(path):
        if not isinstance(row, dict):
            continue
        wid, host_uid, peers = row.get("workspace_id"), row.get("host_uid"), row.get("peers")
        if not isinstance(wid, str) or not WORKSPACE_ID.fullmatch(wid) or not isinstance(peers, dict):
            continue
        if isinstance(host_uid, bool) or host_uid != uid:
            continue
        peer = peers.get(str(uid))
        person = peer.get("person") if isinstance(peer, dict) and peer.get("class") == "person" else None
        if isinstance(person, str) and person:
            people[wid] = person
    return people


# The inner ops of the settings hand-offs (M4). `grant` (issuing) is not here: the signer draws the secret.
WRITE_OPS = (
    "move",
    "settings.set",
    "policy.set",
    "member.add",
    "member.role",
    "member.remove",
    "device.remove",
    "grant.revoke",
    "addon.grant",
    "addon.disable",
    "addon.purge",
)


#: what the Add-ons page calls (all of it)
MARKET_PAGE_OPS = (
    "market.list",
    "market.installed",
    "market.stage",
    "market.unstage",
    "market.uninstall",
    "market.refresh",
    "market.add",
)
#: the host ops the Publish page and box need (shares.py); all under cap `publish/1`
PUBLISH_FEATURE_OPS = (
    "publish.list",
    "publish.prepare",
    "publish.send",
    "publish.revoke",
    "publish.discard",
    "publish.register",
)


class NextFlight:
    """At most one run of ``fn`` at a time, and at most one waiting behind it, however many callers ask.

    A page load asks for the workspace list several times (the feature list, the viewer, the workspaces) and a page
    the person reloads leaves its requests behind: the server cannot tell a closed page from an open one, so each
    abandoned request still ran its full round of host calls, one after another on the same host connection. Ten
    quick reloads made the next page wait for forty runs. Now a caller that arrives while a run is under way waits for
    the NEXT run (one that starts after it asked, so it never reads older than a run of its own would), and every
    caller that arrived meanwhile shares that one. A run's error reaches everyone who shared it.
    """

    def __init__(self, fn):
        self.fn = fn
        self.cond = threading.Condition()
        self.running = False
        self.started = 0  # runs begun
        self.finished = 0  # runs ended
        self.outcome = (None, None)  # (value, error) of the last run

    def __call__(self):
        with self.cond:
            target = self.started + 1  # the first run that starts after this call
            while True:
                if self.finished >= target:
                    value, error = self.outcome
                    break
                if not self.running and self.started + 1 == target:
                    self.running = True
                    self.started = target
                    self.cond.release()
                    try:
                        try:
                            outcome = (self.fn(), None)
                        except BaseException as exc:  # noqa: BLE001 - shared with the callers, then raised in each
                            outcome = (None, exc)
                    finally:
                        self.cond.acquire()
                    self.running = False
                    self.finished = target
                    self.outcome = outcome
                    self.cond.notify_all()
                    value, error = outcome
                    break
                self.cond.wait()
        if error is not None:
            raise error
        return value


def hub_mode(link):
    from .hubapprove import mode_of_link  # lazy: hubapprove imports this module's types

    return mode_of_link(link)


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

    def people(self):
        """``{workspace id: person}``: who this computer's user is in each pinned workspace (the registry pins, no host
        call). Empty when the registry cannot be read; a malformed row is skipped (enrolment must never fail on it)."""
        try:
            return read_people(self.state / "host/workspaces.json")
        except (OSError, ValueError, HostError):
            return {}

    def workspace_folder(self, ws):
        """``(project folder, store folder)`` of workspace ``ws`` from the trusted host registry (the store is the
        project's ``.orch``). The File Explorer's root; browser input never names it."""
        row = self._pins().get(ws)
        if row is None or not isinstance(row.get("root"), str) or not row["root"].startswith("/"):
            raise fail("not_found", exit=2)
        project = Path(row["root"])
        from orch.layout import STORE_DIR

        return project, project / STORE_DIR

    def check_repo_path(self, ws, path):
        """The host's own path check for a repository to add (home folder, not a git folder, inside orch's data), run
        before anything is prepared or signed: the person reads the plain refusal in the dialog. Raises HostError.
        The host checks again when the request is made (this is the same function, on this computer)."""
        from orch import repopaths
        from orch.layout import Layout

        row = self._pins().get(ws)
        if row is None or not isinstance(row.get("root"), str):
            return  # not known here: the host's own check still runs
        layout = Layout.new_at(row["root"])
        try:
            repopaths.check_declared(layout, path)
        except repopaths.RepoPathError as exc:
            raise HostError({"code": "invalid.input", "message": str(exc), "retryable": False}, exit=5) from None

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
        addons = self.addon_rows(link)
        if not any(a["name"] == "dashboard" and a["enabled"] is True for a in addons):
            raise fail("addon.disabled", exit=3)
        return link

    def paused_unverified(self, ws) -> bool:
        """Is the dashboard add-on only paused because a joined hub has not confirmed its first snapshot yet? Read-only,
        for the words the page shows; the pause itself (fail closed, hubpolicy) is the host's and is not touched."""
        try:
            row = next((a for a in self.link(ws).call("addon.list")["addons"] if a.get("name") == "dashboard"), {})
        except (HostError, KeyError, TypeError, AttributeError):
            return False
        paused = row.get("paused")
        return row.get("state") == "paused" and isinstance(paused, dict) and paused.get("cause") == "unverified"

    @staticmethod
    def addon_rows(link):
        """`addon.list` once per HTTP request (a write clears it): the gates of one request share one read."""
        memo = REQUEST_MEMO.get()
        if memo is None or memo.get("closed"):
            return link.call("addon.list")["addons"]
        key = ("addon.list", id(link))
        if key not in memo:
            memo[key] = link.call("addon.list")["addons"]
        return copy.deepcopy(memo[key])

    #: the last state `addon.list` gave per (link, add-on): a transient error keeps it instead of reading as off
    _addon_seen: dict = {}

    @classmethod
    def addon_on(cls, link, name) -> bool:
        """Is the shipped add-on `name` granted and enabled here (`addon.list` state `active`)? An answer that cannot be
        read keeps the last known state (a dropped host must not hide the page, or a half-finished share, for a
        moment); with none known it counts as off."""
        welcome = getattr(link, "welcome", None)
        ws = (welcome.get("workspace") or {}).get("id") if isinstance(welcome, dict) else None
        key = (ws or id(link), name)
        try:
            rows = cls.addon_rows(link)
        except (HostError, KeyError, TypeError):
            return cls._addon_seen.get(key, False)
        on = any(isinstance(a, dict) and a.get("name") == name and a.get("state") == "active" for a in rows)
        cls._addon_seen[key] = on
        return on

    @classmethod
    def capability_on(cls, link, capability) -> bool:
        """Does an add-on that is granted, enabled and holds `capability` exist here? The decision is by the active
        grant's capability, never by the add-on's name. Unreadable: the last known answer, else off."""
        welcome = getattr(link, "welcome", None)
        ws = (welcome.get("workspace") or {}).get("id") if isinstance(welcome, dict) else None
        key = (ws or id(link), "capability:" + capability)
        try:
            rows = cls.addon_rows(link)
        except (HostError, KeyError, TypeError):
            return cls._addon_seen.get(key, False)
        on = any(
            isinstance(a, dict) and a.get("state") == "active" and capability in (a.get("capabilities") or ())
            for a in rows
        )
        cls._addon_seen[key] = on
        return on

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
            # V3 A10: the Add-ons page (Browse, Installed, Marketplaces); every install is a terminal hand-off
            "market": [(op, "ops/1") for op in MARKET_PAGE_OPS],
            # V3 A7: the Hub tab, the top-bar chip and the Tickets column (reads; every change is a terminal command).
            # R2 (#1590): Settings > Devices > Remote access (the page also needs a session opened on this computer).
            "remote": [("remote.status", "ops/1")],
            "hub": [
                ("hub.status", "hub/1")
            ],  # share.* is checked by its own routes: an older host degrades in the page
        }
        for feature, ops in requirements.items():
            if all(link.supports(op, cap) for op, cap in ops):
                features.append(feature)
        if all(link.supports(op, "publish/1") for op in PUBLISH_FEATURE_OPS) and self.addon_on(link, "publish"):
            # Publish (P6): the page and the ticket's Shares box exist only while the add-on is ON for this workspace.
            features.append("publish")
        if "terminals" in features and self.addon_on(link, "terminals"):
            # The Terminals page (menu entry and route) exists only while the add-on is ON; the dock keeps its own gate.
            features.append("terminals_page")
        if "agent_start" in features and self.capability_on(link, "choose_model"):
            features.append("model_routing")  # an active add-on holds choose_model: the model choice is offered
        if link.supports("run.list", "run/1") and self.addon_on(link, "usage"):
            # Usage: the page and the ticket box exist only while the add-on is ON for this workspace.
            features.append("usage")
        if link.supports("follow_up.list", "ops/1") and self.addon_on(link, "schedules"):
            features.append("schedules")  # Follow-ups (#720)
        if self.addon_on(link, "focus"):
            features.append("focus")
        if self.addon_on(link, "wiki"):
            features.append("wiki")
            features.append("addons")  # declarative add-on pages (the Wiki view) are served
        if "quick" in features and self.addon_on(link, "quick"):
            features.append("quick_on")  # the menu entry and the page's controls; Off: the page says so
        if self.addon_on(link, "estimate"):
            # Estimate: points on tickets (the ticket's size), shown and set only while the add-on is ON here.
            features.append("estimate")
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

    def one_click_on(self, ws) -> bool:
        """Is click-to-sign switched ON for this workspace (the signer's flag)? Only the default sign method follows
        it: the terminal hand-off stays available either way (#1170)."""
        card = self.click.status().get(ws)
        return isinstance(card, dict) and card.get("enabled") is True

    def workspaces(self):
        """The workspaces this person may see, read from the host. Concurrent callers share runs (``NextFlight``)."""
        flight = self.__dict__.get("_workspace_flight")
        if flight is None:
            flight = self.__dict__.setdefault("_workspace_flight", NextFlight(self._workspaces))
        return flight()

    def _workspaces(self):
        with self.lock:
            pins = self._pins()
            if self.control is None or self.control.sock.fileno() < 0:
                self.control = HostLink.connect(self.run / "control/host.sock", os.getuid(), None)
            rows = self.control.call("host.list")["workspaces"]
        result = []

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
                if role == "owner" and self.addon_on(link, "explorer"):
                    features.append("explorer")  # the File Explorer: owners only, and only while the add-on is ON
                count = adapters.actionable_count(link.call("needs")["items"], role) if "today" in features else None
                if (
                    not isinstance(show.get("name"), str)
                    or not isinstance(show.get("prefix"), str)
                    or hidden(show["name"])
                    or hidden(show["prefix"])
                ):
                    # Not repaired, not shown: invisible or bidi characters could make a name pass for another.
                    raise fail("workspace.name_invalid", "The workspace name has invisible characters.", exit=3)
                icon, icon_known = (
                    adapters.shell.workspace_icon(show),
                    "icon" in show,
                )  # D6: unsigned, from workspace.show
                result.append(
                    adapters.Workspace(
                        icon=icon,
                        icon_known=icon_known,
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
                        signing=hub_mode(link),
                    )
                )
            except HostError as exc:
                # No untrusted title or principal is fabricated when the host is unavailable.
                result.append(
                    adapters.Workspace(
                        id=ws,
                        prefix=adapters.pinned_prefix(pins[ws]),
                        name=ws,
                        host_state="addon.paused"
                        if exc.error["code"] == "addon.disabled" and self.paused_unverified(ws)
                        else exc.error["code"],
                        host_message=adapters.unavailable_reason(exc.error),
                    )
                )
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
