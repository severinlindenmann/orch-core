"""The setup flow: phases, the routes of the contract (batch-9.md "## Contract") and the orchestration."""

from __future__ import annotations

import re
import secrets as _secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude_folder
from . import facts as facts_mod
from . import review as review_mod
from .protocol import AGENT_HOURS, METHODS, SetupError, validate_profile
from .steps import STEP_LABELS, Creator, Run, Secrets
from .tunnel import is_headless, tunnel_line

__all__ = ["Env", "SetupFlow"]


def _builtin_manifest(name: str) -> bytes:
    from orch.addons import builtin

    return builtin.manifest_bytes(name)


@dataclass
class Env:
    """Everything the flow reads from the machine, as seams (the tests replace them; production fills the defaults
    in :func:`orch.setup.cli.default_env`)."""

    env: Mapping[str, str]
    home: Path
    state_dir: Path
    config_dirs: tuple[Path, ...] = ()
    #: root -> a registry entry (or None): a registered directory is "Finish setup"
    registered: Callable[[str], Any] = lambda root: None
    catalog: Callable[[], list[facts_mod.AddonInfo]] = facts_mod.catalog
    #: (available, reason code): can this machine hold a convenience (one-click) key?
    click: Callable[[], tuple[bool, str | None]] = lambda: (True, None)
    #: {available, enabled}: the relay add-on, only offered when it is both
    relay: Callable[[], dict[str, bool]] = lambda: {"available": False, "enabled": False}
    #: name/prefix check for reserved prefixes (SBX): raises an OrchError-like on refusal
    reserved: Callable[[Path, str, str], None] = lambda root, prefix, name: None
    #: the bytes of a built-in add-on's manifest (the same bytes `orch addon manifest` writes)
    manifest_bytes: Callable[[str], bytes] = lambda name: _builtin_manifest(name)
    #: Finish setup: the facts of the registered workspace (read inside store.locked())
    read_facts: Callable[[Any], facts_mod.Facts] | None = None
    #: the real or a fake :class:`orch.setup.steps.Ports`
    ports: Callable[[], Any] | None = None
    #: where "Open the workspace" goes after Finish (the dashboard route of the workspace)
    workspace_url: Callable[[str], str] = lambda wid: "/"
    clock: Callable[[], float] = time.time


@dataclass
class _Review:
    id: str
    plan: review_mod.Plan


class SetupFlow:
    def __init__(self, env: Env) -> None:
        self.e = env
        self.lock = threading.RLock()
        self.phase = "profile"
        self.mode = "new"
        self.dir = ""
        self.root: Path | None = None
        self.entry: Any = None
        self.facts: facts_mod.Facts | None = None
        self.addons = {a.id: a for a in env.catalog()}
        self.profile: dict[str, Any] | None = None
        self.secrets: Secrets | None = None
        self.review: Any = None
        self.run: Run | None = None
        self.error: dict[str, Any] | None = None
        self._running = False
        self._creator: Creator | None = None
        self._closed = False
        self.cancel_event = threading.Event()
        #: a line for the person's terminal (the setup server sets it); the page never sees these words
        self.on_note: Callable[[str], None] = lambda line: None
        self.thread: threading.Thread | None = None

    # ---------------------------------------------------------------------------------------------- server protocol
    @property
    def busy(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        """The session is over: cancelled, or the person pressed Finish (``done`` alone keeps the page working)."""
        return self.phase == "cancelled" or self._closed

    @property
    def outcome(self) -> str:
        """``done`` when the workspace was set up and the person pressed Finish, else ``paused``."""
        return "done" if self._closed and (self.ready or self.complete) else "paused"

    @property
    def ready(self) -> bool:
        """The workspace is set up (whether or not the page pressed Finish)."""
        return self.run is not None and self.run.done

    @property
    def complete(self) -> bool:
        """Finish setup on a workspace that lacks nothing (B12 N9): there is nothing to do but open the dashboard."""
        return (
            self.mode == "finish"
            and self.run is None
            and self.facts is not None
            and not facts_mod.all_missing(self.facts, self.addons)
        )

    def launch(self, directory: str) -> None:
        with self.lock:
            if self._running:  # K2: a new session never replaces a run in progress
                raise SetupError("setup.busy", "setup is creating the workspace", 409)
            real, mode = facts_mod.check_dir(
                directory,
                home=self.e.home,
                state_dir=self.e.state_dir,
                config_dirs=self.e.config_dirs,
                registered=self.e.registered,
            )
            if mode == "finish":
                self.entry = self.e.registered(str(real))
                if self.e.read_facts is None:
                    raise SetupError("setup.facts_unreadable", "cannot read this workspace", 500)
                self.facts = self.e.read_facts(self.entry)
            self.root, self.dir, self.mode = real, str(real), mode
            self._drop_secrets()
            self.profile, self.review, self.phase, self.error = None, None, "profile", None

    def end(self, why: str) -> None:
        with self.lock:
            self._drop_secrets()
            if self.phase not in ("done", "cancelled"):
                self.phase = "cancelled"
                self.error = {"code": f"setup.{why}", "message": f"the setup ended ({why})"}

    # ---------------------------------------------------------------------------------------------------- routes
    def dispatch(self, method: str, route: str, query: dict[str, str], body: Any) -> tuple[int, dict[str, Any]]:
        handler = self._routes().get((method, route))
        if handler is None:
            raise SetupError("setup.not_found", "no such route", 404)
        with self.lock:
            if self.phase == "cancelled" and route != "state":
                raise SetupError("setup.ended", "this setup has ended", 410)
            return 200, handler(body if method == "POST" else query)

    def _routes(self) -> dict[tuple[str, str], Callable[[Any], dict[str, Any]]]:
        return {
            ("GET", "state"): self._state,
            ("POST", "profile"): self._post_profile,
            ("POST", "secrets/reveal"): self._reveal,
            ("POST", "secrets/confirm"): self._approve,
            ("POST", "review"): self._post_review,
            ("POST", "create"): self._create,
            ("GET", "progress"): self._progress,
            ("POST", "finish"): self._finish,
            ("POST", "cancel"): self._cancel,
            ("POST", "retry"): self._retry,
        }

    def _state(self, _q: Any) -> dict[str, Any]:
        from orch.custody.cli import RISK as CONVENIENCE_RISK

        click_ok, click_reason = self.e.click()
        relay = self.e.relay()
        assert self.root is not None or not self.dir
        from . import facts
        from .osfacts import _platform

        sug = (
            facts.suggest(dict(self.e.env), Path(self.dir))
            if self.dir
            else {"name": "", "workspace_name": "", "prefix": ""}
        )
        if self.facts is not None:
            sug = {
                "name": self.facts.name,
                "workspace_name": Path(self.dir).name or self.facts.prefix,
                "prefix": self.facts.prefix,
            }
        if self.mode != "finish":  # U1: a new workspace starts with "let agents work for you" ticked (3 days)
            sug = {**sug, "agent_hours": facts.DEFAULT_AGENT_HOURS, "agent_grant": True}
            # run 8 (owner): One click is the default where this machine can hold the key; Terminal stays selectable.
            # Only a suggestion: the profile is still validated, reviewed (with the risk sentence) and signed as before.
            sug = {**sug, "method": "click" if click_ok else "terminal"}
        missing: list[str] = []
        current: dict[str, Any] | None = None
        if self.mode == "finish" and self.facts is not None:
            missing = (
                facts.missing_items(self.facts, self.profile, self.addons)
                if self.profile is not None
                else facts.all_missing(self.facts, self.addons)
            )
            current = facts.current(self.facts, self.addons)
        return {
            "phase": self.phase,
            "mode": self.mode,
            "dir": self.dir,
            "dir_name": Path(self.dir).name if self.dir else "",
            "suggested": sug,
            "addons": [a.view() for a in self.addons.values()],
            "methods": list(METHODS),
            "os": _platform(),  # the machine running setup: the page words things only for the OS they are true on
            "click_available": bool(click_ok),
            "click_reason": click_reason,
            "click_risk": CONVENIENCE_RISK,
            "agent_hours": list(AGENT_HOURS),
            "git": facts.git_state(Path(self.dir)) if self.dir else {"repo": True},
            "claude": {
                "available": bool(self.dir)
                and self.mode == "new"
                and claude_folder.offered(self.e.env, self.dir, allow_parent=True),
                "why_not": (claude_folder.refusal(self.e.env, self.dir, allow_parent=True) or "") if self.dir else "",
                # config above the folder: shown by path, trusted only on the explicit "trust anyway"
                "parent_config": (claude_folder.parent_config(self.dir, self.e.env) or "") if self.dir else "",
            },
            "relay": {"available": bool(relay.get("available")), "enabled": bool(relay.get("enabled"))},
            "missing": missing,
            "missing_words": [facts.label(m, self.addons) for m in missing],
            "current": current,
            "complete": self.complete,
            "labels": {k: facts.label(k, self.addons) for k in (*facts.LABELS, *(f"addon:{a}" for a in self.addons))},
            "error": self.error,
        }

    # -- profile (R5, reserved prefix, closed schema); any POST voids a prior review
    def _post_profile(self, body: Any) -> dict[str, Any]:
        if self.phase in ("confirming", "running", "done", "failed"):
            raise SetupError("setup.phase", "the profile can no longer be changed", 409)
        if isinstance(body, dict) and isinstance(body.get("addons"), list):
            # the locked-on add-ons are always part of the profile, whatever the page sent
            req = [a.id for a in self.addons.values() if a.required]
            body = {**body, "addons": list(dict.fromkeys([*body["addons"], *req]))}
        p = validate_profile(
            body,
            known_addons=frozenset(self.addons),
            required=frozenset(a.id for a in self.addons.values() if a.required),
        )
        relay = self.e.relay()
        if p["method"] == "click" and p["relay"]:  # R5, before anything else about the method
            raise SetupError(
                "click_relay_unsupported",
                "one-click approval cannot be combined with the phone relay yet",
                400,
                click_reason="relay",
            )
        if p["relay"] and not (relay.get("available") and relay.get("enabled")):
            raise SetupError("invalid.input", "the relay add-on is not available", 400, field="relay")
        if p["method"] == "click":
            ok, reason = self.e.click()
            if not ok:
                raise SetupError(
                    "click_unavailable", "one-click approval is not available here", 400, click_reason=reason
                )
        if self.mode == "finish":
            assert self.facts is not None
            if (p["name"], p["prefix"]) != (self.facts.name, self.facts.prefix):
                raise SetupError(
                    "invalid.input", "name and prefix of an existing workspace do not change", 400, field="name"
                )
        else:
            assert self.root is not None
            try:
                self.e.reserved(self.root, p["prefix"], p["name"])
            except SetupError:
                raise
            except Exception as exc:  # noqa: BLE001 - OrchError from the shared reserved-prefix check
                code = getattr(exc, "code", "invalid.input")
                if code != "invalid.input":
                    raise
                raise SetupError(
                    "invalid.input", str(getattr(exc, "message", "prefix refused"))[:200], 400, field="prefix"
                ) from None
        if p["git_init"] and (self.mode != "new" or not self.dir or facts_mod.git_state(Path(self.dir))["repo"]):
            raise SetupError("invalid.input", "git init is offered for a new workspace in a folder without git", 400,
                             field="git_init")  # fmt: skip
        if p["trust_folder"] and (
            self.mode != "new"
            or not self.dir
            or not claude_folder.offered(self.e.env, self.dir, allow_parent=bool(p["trust_parent"]))
        ):
            raise SetupError("invalid.input", "Claude Code trust is not offered for this folder", 400,
                             field="trust_folder")  # fmt: skip
        prior = self.profile
        self.profile, self.review = p, None  # a new profile voids every earlier review_id
        if self.mode == "finish":
            self.phase = "review"
        else:
            # the passphrase is shown on the terminal path only: a changed method after the reveal draws new secrets
            if self.secrets is not None and (prior is None or prior["method"] != p["method"]):
                self._drop_secrets()
            self.phase = "secrets" if not (self.secrets and self.secrets.confirmed) else "review"
        return {"ok": True}

    # -- review (R1): rendered in core from the profile; the digest binds what `create` may run
    def _post_review(self, _body: Any) -> dict[str, Any]:
        if self.profile is None or self.phase != "review":
            raise SetupError("setup.phase", "the review comes after the profile and the secrets", 409)
        if self.mode == "new" and not (self.secrets and self.secrets.confirmed):
            raise SetupError("secrets.not_confirmed", "confirm the recovery code first", 409)
        missing = None
        if self.mode == "finish":
            assert self.facts is not None
            missing = facts_mod.missing_items(self.facts, self.profile, self.addons)
        from orch import layout as layout_mod
        from orch import privdir

        home = Path(self.e.env.get("HOME") or Path.home())
        shown = [
            privdir.shown(p, home)
            for p in privdir.setup_paths(Path(self.dir), layout_mod.store_of(self.dir), self.e.state_dir, home)
        ]
        plan = review_mod.plan(
            self.profile,
            self.dir,
            self.addons,
            self.facts,
            manifest_bytes=self.e.manifest_bytes,
            missing=missing,
            private_paths=shown,
        )
        rid = "rv_" + _secrets.token_hex(8)
        self.review = _Review(rid, plan)
        return plan.view(rid)

    # -- create: the run (R1 digest, R2 word), then the sequence in a worker thread
    def _create(self, body: Any) -> dict[str, Any]:
        if self.review is None or self.phase != "review":
            raise SetupError("review.changed", "there is no review to create from; review again", 409)
        if not isinstance(body, dict):
            raise SetupError("invalid.input", "create takes review_id and digest", 400)
        plan = self.review.plan
        if body.get("review_id") != self.review.id or body.get("digest") != plan.digest:
            raise SetupError("review.changed", "the review changed; read it again", 409)
        if self.mode == "new" and not (self.secrets and self.secrets.confirmed):
            raise SetupError("secrets.not_confirmed", "confirm the recovery code first", 409)
        if self.mode == "finish":  # R7: the facts are read again; a different answer than the review's is a new review
            assert self.facts is not None and self.e.read_facts is not None and self.profile is not None
            fresh = self.e.read_facts(self.entry)
            if facts_mod.missing_items(fresh, self.profile, self.addons) != facts_mod.missing_items(
                self.facts, self.profile, self.addons
            ):
                raise SetupError("review.changed", "the workspace changed since the review; review again", 409)
        assert self.profile is not None and self.root is not None
        ids = self._step_ids(plan)
        run = Run("run_" + _secrets.token_hex(6), [(i, self._step_label(i)) for i in ids])
        self.run = run
        ports = self.e.ports() if self.e.ports else None
        if ports is None:
            raise SetupError("setup.no_ports", "setup cannot reach the host", 500)
        wid = self.entry["workspace_id"] if self.mode == "finish" else None
        creator = Creator(
            ports,
            plan=plan,
            profile=self.profile,
            root=self.root,
            secrets=self.secrets,
            run=run,
            mode=self.mode,
            wid=wid,
            clock=self.e.clock,
            cancelled=self.cancel_event,
            manifest_bytes=self.e.manifest_bytes,
        )
        self._creator = creator
        self.phase = "confirming" if plan.tty_confirm else "running"
        self._running = True
        self.thread = threading.Thread(target=self._work, args=(creator,), name="orch-setup-run")
        self.thread.start()
        return {"run_id": run.id}

    def _step_label(self, step_id: str) -> str:
        """A step in words: the fixed ones by name, an add-on as ``<title> add-on`` (never ``addon:dashboard``)."""
        if step_id in STEP_LABELS:
            return STEP_LABELS[step_id]
        if step_id.startswith("addon:") and step_id[6:] in self.addons:
            return f"{self.addons[step_id[6:]].title} add-on"
        return facts_mod.label(step_id, self.addons)

    def _step_ids(self, plan: review_mod.Plan) -> list[str]:
        ids = [] if self.mode == "finish" else ["keys", "genesis", "register"]
        if any(f["id"] == "perms" for f in plan.flags):
            ids.insert(0, "perms")
        if any(f["id"] == "git" for f in plan.flags):
            ids.append("git")
        if any(f["id"] == "runner" for f in plan.flags):
            ids.append("runner")
        ids += [i.id for i in plan.items if i.id != "runner_grant"]
        if any(i.id == "runner_grant" for i in plan.items):
            ids.append("runner_grant")
        if self.profile and self.profile["method"] == "click":
            ids += ["custody", "flags"]
        if any(f["id"] == "records" for f in plan.flags):
            ids.append("records")
        if any(f["id"] == "trust" for f in plan.flags):
            ids.append("trust")
        return ids

    def _retry(self, _body: Any) -> dict[str, Any]:
        creator = self._creator
        if self.phase != "failed" or creator is None or self._running or self.run is None:
            raise SetupError("setup.phase", "there is nothing to try again", 409)
        if self.run.error is None or self.run.error.get("code") not in ("background.blocked", "steps.failed"):
            raise SetupError("setup.phase", "there is nothing to try again", 409)
        self.phase, self._running = "running", True
        self.thread = threading.Thread(target=self._work, args=(creator, True), name="orch-setup-retry")
        self.thread.start()
        return {"run_id": self.run.id}

    def _work(self, creator: Creator, again: bool = False) -> None:
        try:
            creator.retry() if again else creator.execute()
        finally:
            with self.lock:
                self._running = False
                if self.phase != "cancelled":
                    self.phase = "done" if creator.run.done else "failed"
                    if creator.run.done:
                        self.error = None

    def _progress(self, query: Any) -> dict[str, Any]:
        if self.run is None or query.get("run_id") != self.run.id:
            raise SetupError("setup.run_unknown", "no such run", 404)
        out = self.run.view()
        if self.phase in ("confirming", "running") and not self.run.waiting_tty:
            self.phase = "running"
        elif self.run.waiting_tty:
            self.phase = "confirming"
        return out

    def _finish(self, _body: Any) -> dict[str, Any]:
        if not (self.ready or self.complete) or self.phase == "cancelled":
            raise SetupError("setup.phase", "setup is not finished", 409)
        if self.mode == "new" and (self.run is None or self.run.workspace is None):
            raise SetupError("setup.phase", "setup is not finished", 409)
        wid = ((self.run.workspace if self.run is not None else None) or {}).get("id") or (self.entry or {}).get(
            "workspace_id", ""
        )
        # The link first, the end of the session after it: a dashboard that did not open leaves this page working, so
        # "Try again" reaches this setup once more (r11-P01). The cause goes to the terminal, never to the page.
        try:
            url = self.e.workspace_url(wid)
        except Exception as e:  # noqa: BLE001 - any failure here is the same retryable answer
            why = e.message if isinstance(e, SetupError) else (str(e) or type(e).__name__)
            self.on_note(f"the dashboard did not open yet: {why[:200]}; the page offers Try again")
            raise SetupError(
                "setup.dashboard_unavailable", "the dashboard did not open yet; try again", 503, retry=True
            ) from None
        self._closed = True
        return {"url": url, "workspace_id": str(wid)}

    def cancel_run(self) -> None:
        """Ctrl-C on the setup terminal: the run stops between steps (a conversion in flight is undone)."""
        self.cancel_event.set()

    # -- secrets
    def _reveal(self, _body: Any) -> dict[str, Any]:
        if self.mode != "new" or self.profile is None or self.phase not in ("secrets",):
            raise SetupError("setup.phase", "the secrets come after the approval step", 409)
        if self.secrets is None:
            self.secrets = Secrets(show_passphrase=self.profile["method"] == "terminal")
        return self.secrets.reveal()

    def _approve(self, body: Any) -> dict[str, Any]:
        if self.phase != "secrets":
            raise SetupError("setup.phase", "the recovery words are acknowledged right after they are shown", 409)
        if self.secrets is None:
            raise SetupError("secrets.not_shown", "show the secrets first", 409)
        if not isinstance(body, dict):
            raise SetupError("invalid.input", "acknowledged", 400, field="acknowledged")
        out = self.secrets.confirm(body.get("acknowledged"))
        self.phase = "review"
        return out

    def _drop_secrets(self) -> None:
        if self.secrets is not None:
            self.secrets.wipe()
            self.secrets = None

    def _cancel(self, _body: Any) -> dict[str, Any]:
        if self._running:
            raise SetupError("setup.busy", "setup is creating the workspace; stop it with Ctrl-C", 409)
        self._drop_secrets()
        self.phase = "cancelled"
        return {"ok": True}


# ======================================================================================================== the command


def default_env(environ: Mapping[str, str]) -> Env:
    """The real machine: registry, custody availability, the store for Finish setup, the real host ports."""
    import os

    from orch.ops.runtime import state_dir

    from . import steps

    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    state = state_dir(environ)
    uid = os.getuid()

    def entries() -> list[dict[str, Any]]:
        from orch.host.signer_registry import _own_workspaces

        try:
            return _own_workspaces(state, uid, None)
        except Exception:  # noqa: BLE001 - an unreadable registry: no workspace is "registered" (the dir check then
            return []  # refuses a directory that holds store files)

    def registered(root: str) -> Any:
        for e in entries():
            if os.path.realpath(str(e.get("root"))) == root:
                return e
        return None

    def click() -> tuple[bool, str | None]:
        return True, None

    def read_facts(entry: Any) -> facts_mod.Facts:
        from orch.custody import select
        from orch.runner import enablement
        from orch.store import Store

        wid, person = str(entry["workspace_id"]), str(entry["peers"][str(uid)]["person"])
        try:
            store = Store.open(entry["root"], expected_workspace_id=wid, host_state_dir=state, load="lazy")
        except Exception:  # noqa: BLE001
            raise SetupError("setup.facts_unreadable", "the workspace could not be opened", 500) from None
        try:
            confirmed = False
            try:
                link = steps.RealPorts(environ, state, uid).workspace(wid)
                try:
                    runner = (link.call("whoami", {}).get("data") or {}).get("runner") or {}
                    confirmed = runner.get("grant") is not None
                finally:
                    link.close()
            except Exception:  # noqa: BLE001 - not confirmed unless the host says so
                confirmed = False
            return facts_mod.read_facts(
                store,
                person,
                runner=enablement.state_of(state, wid, uid),
                deposit_confirmed=confirmed,
                convenience=select.status(state, wid).get("custody") == "convenience",
            )
        finally:
            store.close()

    def reserved(root: Path, prefix: str, name: str) -> None:
        from orch.ops.workspace_init import check_reserved_prefix

        check_reserved_prefix(root, state, prefix, name)

    return Env(
        env=environ,
        home=home,
        state_dir=state,
        config_dirs=(home / ".config" / "orch",),
        registered=registered,
        click=click,
        reserved=reserved,
        read_facts=read_facts,
        ports=lambda: steps.RealPorts(environ, state, uid),
    )


_LINK = re.compile(r"http://127\.0\.0\.1:\d{1,5}/#launch=[A-Za-z0-9_-]{20,100}&to=%2Fsetup")
USAGE = (
    "usage: orch setup [--no-browser] [--role workspace|full|pick] [--address ADDRESS]\n"
    "  (run it in the folder you want to make a workspace; it asks what this computer should be, then opens the page)\n"
)


def _open_browser(environ: Mapping[str, str], err: Any, launch: dict[str, Any], *, no_browser: bool = False) -> Any:
    """Open the dashboard on ``/setup`` and show the same single-use link on the person's own terminal (/dev/tty,
    never ``err``). The launch code is NOT in any argument or in the environment of a child: the launcher returns the
    link over a pipe (``--emit-url-fd``, a number only) and opens the browser through its own 0600 redirect file. A
    missing dashboard says how to install it; one that fails to start says why (its last error line, with any URL
    fragment removed so no one-time code is ever echoed). Returns ``True`` (browser opened), ``"link"`` (the link was
    shown but no browser opened) or ``False``."""
    import os
    import subprocess

    from orch.cli.dashboard import find_entry_point

    cmd = find_entry_point(environ, err)
    if cmd is None:
        return False
    read_fd, write_fd = os.pipe()
    try:
        try:
            res = subprocess.run(
                [*cmd, "open", "/setup", "--emit-url-fd", str(write_fd), *(["--no-browser"] if no_browser else [])],
                env=dict(environ),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
                check=False,
                pass_fds=(write_fd,),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            err.write(f"orch setup: the dashboard did not start ({type(exc).__name__})\n")
            return False
        finally:
            os.close(write_fd)
        if res.returncode != 0:
            text = (res.stderr or b"").decode("utf-8", "replace") + (res.stdout or b"").decode("utf-8", "replace")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            last = re.sub(r"#\S*", "#…", lines[-1])[:300] if lines else f"exit {res.returncode}"
            err.write(f"orch setup: the dashboard did not start: {last}\n")
            return False
        with os.fdopen(read_fd, "rb", closefd=False) as pipe:
            raw = pipe.read(4097).decode("utf-8", "replace").split("\n")
    finally:
        os.close(read_fd)
    line = raw[0].strip()
    browser_opened = "no-browser" not in [x.strip() for x in raw[1:]]
    if not _LINK.fullmatch(line):
        err.write("orch setup: the dashboard returned no usable link\n")
        return False
    # The link carries the single-use code, so it goes to the person's own terminal (/dev/tty) and nowhere else: not
    # ``err`` or stdout, which can be redirected into a file or a CI log (``orch setup 2>log``).
    from orch.custody import NoPrompt
    from orch.ops import workspace_init as wi

    lead = "If no browser opened, use this link" if browser_opened else "Open this link in your browser"
    hint = ""
    if is_headless(
        environ
    ):  # no browser here: the person reaches the link through an ssh tunnel from their own computer
        port = int(line.split("://127.0.0.1:", 1)[1].split("/", 1)[0])
        hint = (
            f"On your own computer, first run:\n  {tunnel_line(port, environ)}\nthen open this link in its browser.\n"
        )
    try:
        wi.TERMINAL.show(hint + f"{lead} (single use, 5 minutes): {line}\n")
    except (NoPrompt, OSError):
        err.write("orch setup: cannot show the link here; run orch setup in your own terminal\n")
        return True if browser_opened else False
    return True if browser_opened else "link"


def main(
    argv: list[str],
    *,
    env: Mapping[str, str],
    out: Any,
    err: Any,
    make_env: Callable[[Mapping[str, str]], Env] | None = None,
    ensure_host: Callable[[Mapping[str, str], Any], None] | None = None,
    open_browser: Callable[[Mapping[str, str], Any, dict[str, Any]], bool] | None = None,
    enter_pressed: Callable[..., bool] | None = None,
    server_options: Mapping[str, Any] | None = None,
    run_dir: Path | None = None,
    cwd: str | None = None,
    install_ctx: Any = None,
) -> int:
    """``orch setup``: the SETUP PROCESS. A person's command: refused under an agent's environment and without a
    terminal; needs the host (``host enable`` once, dev checkouts refused there); stays running until the page is done,
    cancelled, or Ctrl-C. Exit 0 only when the workspace was set up."""
    import os

    from orch.ops import workspace_init as wi

    from .server import SetupServer
    from .steps import set_no_core_dumps

    no_browser, role_arg, address_arg = False, None, None
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if flag == "--no-browser":
            no_browser = True
        elif (
            flag in ("--role", "--address") and rest and (flag != "--role" or rest[0] in ("workspace", "full", "pick"))
        ):
            if flag == "--role":
                role_arg = rest.pop(0)
            else:
                address_arg = rest.pop(0)
        else:
            err.write(USAGE)
            return 2
    if "ORCH_GRANT" in env or env.get("ORCH_SESSION"):
        err.write("orch setup: setting up a workspace is a person's job; an agent never does it\n")
        return 3
    if not wi.TERMINAL.available():
        err.write("orch setup: needs your own terminal (/dev/tty)\n")
        return 3
    from orch.prune.check import older_install

    if (found := older_install(env, mark=True)) is not None:  # an older install's identity must not meet a new setup
        err.write(f"orch setup: {found}\n")
        return 3
    set_no_core_dumps()
    ictx = install_ctx or _install_ctx(env, out, err, ask=role_arg is None)
    if ictx is not None and (role_arg is not None or ictx.interactive) and not _server_installed(ictx):
        from orch.install import cli as icli
        from orch.install.parts import InstallError

        role, server_parts = icli.choose_role(ictx, role_arg)
        try:
            icli.full_server(ictx, server_parts, address=address_arg)
        except InstallError as e:
            err.write(f"orch setup: {e}\n")
            return 1
        if server_parts:
            out.write(f"orch setup: {role}: {', '.join(server_parts)} installed; now this workspace\n")
    denv = (make_env or default_env)(env)
    if ictx is not None and denv.workspace_url is Env.workspace_url:
        from orch.install import cli as icli

        folder = cwd if cwd is not None else os.path.realpath(os.getcwd())
        # the page's own origin (the setup server) stops with this command: send "Open your workspace" to the running
        # dashboard service instead, with a fresh login (LoginUnavailable: Finish answers "try again"); a machine
        # without a dashboard service keeps the page's own dashboard (relative "/")
        denv.workspace_url = lambda wid: icli.workspace_login_url(ictx, folder) or "/"
    try:
        (ensure_host or _ensure_host)(env, err)
    except SetupError as e:
        err.write(f"orch setup: {e.message}\n")
        return 1
    # NOT the host run directory: that one is 0711 (peers reach their sockets through it), the setup socket and the
    # launch-code file need a directory only this user can enter.
    sock_dir = run_dir if run_dir is not None else denv.state_dir / "setup"
    flow = SetupFlow(denv)
    flow.on_note = lambda line: err.write(f"orch setup: {line}\n")
    server = SetupServer(
        Path(sock_dir) / "setup.sock",
        flow,
        notify=lambda line: err.write(f"orch setup: {line}\n"),
        **(server_options or {}),
    )
    try:
        server.start()
        launch = server.launch(cwd if cwd is not None else os.path.realpath(os.getcwd()))
    except SetupError as e:
        server.stop()
        err.write(f"orch setup: {e.message}\n")
        return 1
    finishing = "this folder already has a workspace: checking what is still missing"
    kind = finishing if flow.mode == "finish" else "new workspace"
    err.write(f"orch setup: {flow.dir} ({kind})\n")
    opener = open_browser or _open_browser
    opened = opener(env, err, launch, no_browser=True) if no_browser else opener(env, err, launch)
    if opened is True:
        err.write("orch setup: the setup page is open in your browser; this terminal stays with it (Ctrl-C cancels)\n")
    elif opened:
        err.write("orch setup: no browser was opened; use the link above (Ctrl-C cancels)\n")
    else:
        err.write("orch setup: could not open the setup page (see above); fix that and run orch setup again\n")
        server.stop()
        return 1
    try:
        with wi._hangup_as_interrupt():
            wait_enter = enter_pressed or wi.TERMINAL.enter_pressed
            armed = False  # K2: the lost-page notice was printed and Enter now asks for a fresh link
            while not server.stop_event.is_set():
                if not server.lost:
                    armed = False
                    server.stop_event.wait(0.5)
                    continue
                if not armed:
                    armed = True
                    wait_enter(0.0, flush=True)  # what was typed before the notice is not an answer
                if wait_enter(0.5):
                    try:
                        fresh = server.relink()
                    except Exception as e:  # noqa: BLE001 - never leave the terminal waiting on a dead session
                        err.write(
                            f"orch setup: {e.message if isinstance(e, SetupError) else 'could not make a new link'}; "
                            "run orch setup again\n"
                        )
                        server.die("error")
                        break
                    if fresh is not None and not (
                        opener(env, err, fresh, no_browser=True) if no_browser else opener(env, err, fresh)
                    ):
                        err.write("orch setup: could not open the setup page (see above); run orch setup again\n")
                        server.die("error")
    except KeyboardInterrupt:
        flow.cancel_run()
        if flow.thread is not None:
            flow.thread.join(120)
        server.die("cancel")
        err.write("orch setup: cancelled\n")
    finally:
        server.stop()
    ok = flow.phase == "done" or (flow._closed and (flow.ready or flow.complete))
    if ok and ictx is not None:
        from orch.install import cli as icli

        wsport = icli.add_workspace(ictx, flow.dir)
        if wsport is not None:
            dash = icli.load(ictx.layout).get("dashboard")
            if dash:
                out.write(f"workspace dashboard: {dash['url']}  (login link: {icli.dashboard_login_hint(dash)})\n")
            else:
                out.write(f"workspace dashboard port {wsport}: orch dashboard serve --port {wsport}\n")
    if ok and flow.run is not None:
        ws = flow.run.workspace or {}
        out.write(f"workspace {ws.get('prefix', '')} set up in {flow.dir}\n")
    elif ok and flow.facts is not None:
        out.write(f"workspace {flow.facts.prefix} is already set up in {flow.dir}\n")
    return 0 if ok else 1


def _install_ctx(env: Mapping[str, str], out: Any, err: Any, *, ask: bool) -> Any:
    """The seams of ``orch install`` for the role question and the workspace port; ``None`` when this machine has no
    state directory to put them in (nothing asked then)."""
    import sys

    from orch.install import cli as icli

    try:
        ctx = icli.default_ctx(env, out, err)
    except Exception:  # noqa: BLE001 - no HOME / state dir: a plain workspace setup
        return None
    if not ask:
        ctx.interactive = sys.stdin.isatty()
    return ctx


def _server_installed(ictx: Any) -> bool:
    """A full server already exists here: running ``orch setup`` again only adds a workspace."""
    from orch.install.state import load

    return bool(load(ictx.layout)["parts"])


ENABLE_TRIES = 3
ENABLE_RETRY_WAIT = 2.0


def _ensure_host(environ: Mapping[str, str], err: Any) -> None:
    """``host enable`` if no host answers (idempotent); a dev checkout is refused by it."""
    import io

    from orch.host import cli as host_cli
    from orch.ops import workspace_init as wi
    from orch.ops.runtime import state_dir

    state = state_dir(environ)
    try:
        wi.connect_host(state).close()
        return
    except Exception:  # noqa: BLE001 - nobody answers: enable it
        pass
    # right after the parts' services were installed (orch setup --role full) the first start can lose against the
    # service manager still settling: try a few times and say what the last attempt answered.
    buf = io.StringIO()
    for attempt in range(ENABLE_TRIES):
        buf = io.StringIO()
        if host_cli.main(["enable"], env=environ, out=buf) == 0:
            break
        if attempt + 1 < ENABLE_TRIES:
            time.sleep(ENABLE_RETRY_WAIT)
    else:
        why = (buf.getvalue().strip().splitlines() or [""])[0][:200]
        raise SetupError(
            "host.unavailable",
            "the orch host could not be started (orch host enable)" + (f": {why}" if why else ""),
            500,
        )
    for _ in range(40):
        try:
            wi.connect_host(state).close()
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.25)
    raise SetupError("host.unavailable", "the orch host did not come up", 500)
