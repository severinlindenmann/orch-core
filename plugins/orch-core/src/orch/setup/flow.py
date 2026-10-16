"""The setup flow: phases, the routes of the contract (batch-9.md "## Contract") and the orchestration."""

from __future__ import annotations

import secrets as _secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import facts as facts_mod
from . import review as review_mod
from .protocol import AGENT_HOURS, METHODS, SetupError, validate_profile
from .steps import STEP_LABELS, Creator, Run, Secrets

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
        self._closed = False
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None

    # ---------------------------------------------------------------------------------------------- server protocol
    @property
    def busy(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        """The session is over: cancelled, or the person pressed Finish (``done`` alone keeps the page working)."""
        return self.phase == "cancelled" or self._closed

    def launch(self, directory: str) -> None:
        with self.lock:
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
            ("POST", "secrets/confirm"): self._confirm,
            ("POST", "review"): self._post_review,
            ("POST", "create"): self._create,
            ("GET", "progress"): self._progress,
            ("POST", "finish"): self._finish,
            ("POST", "cancel"): self._cancel,
        }

    def _state(self, _q: Any) -> dict[str, Any]:
        click_ok, click_reason = self.e.click()
        relay = self.e.relay()
        assert self.root is not None or not self.dir
        from . import facts

        sug = facts.suggest(dict(self.e.env), Path(self.dir)) if self.dir else {"name": "", "prefix": ""}
        if self.facts is not None:
            sug = {"name": self.facts.name, "prefix": self.facts.prefix}
        missing: list[str] = []
        if self.mode == "finish" and self.facts is not None and self.profile is not None:
            missing = facts.missing_items(self.facts, self.profile, self.addons)
        return {
            "phase": self.phase,
            "mode": self.mode,
            "dir": self.dir,
            "dir_name": Path(self.dir).name if self.dir else "",
            "suggested": sug,
            "addons": [a.view() for a in self.addons.values()],
            "methods": list(METHODS),
            "click_available": bool(click_ok),
            "click_reason": click_reason,
            "agent_hours": list(AGENT_HOURS),
            "relay": {"available": bool(relay.get("available")), "enabled": bool(relay.get("enabled"))},
            "missing": missing,
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
        plan = review_mod.plan(
            self.profile, self.dir, self.addons, self.facts, manifest_bytes=self.e.manifest_bytes, missing=missing
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
        if plan.typed_word is not None and body.get("typed_word") != plan.typed_word:
            raise SetupError("typed_word.required", "type the word shown to continue", 403)
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
        run = Run("run_" + _secrets.token_hex(6), [(i, STEP_LABELS.get(i, i)) for i in ids])
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
        self.phase = "confirming" if plan.tty_confirm else "running"
        self._running = True
        self.thread = threading.Thread(target=self._work, args=(creator,), name="orch-setup-run")
        self.thread.start()
        return {"run_id": run.id}

    def _step_ids(self, plan: review_mod.Plan) -> list[str]:
        ids = [] if self.mode == "finish" else ["keys", "genesis", "register"]
        if any(f["id"] == "runner" for f in plan.flags):
            ids.append("runner")
        ids += [i.id for i in plan.items if i.id != "runner_grant"]
        if any(i.id == "runner_grant" for i in plan.items):
            ids.append("runner_grant")
        if self.profile and self.profile["method"] == "click":
            ids += ["flags", "custody"]
        return ids

    def _work(self, creator: Creator) -> None:
        try:
            creator.execute()
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
        if self.phase != "done" or self.run is None or self.run.workspace is None and self.mode == "new":
            raise SetupError("setup.phase", "setup is not finished", 409)
        wid = (self.run.workspace or {}).get("id") or (self.entry or {}).get("workspace_id", "")
        self._closed = True
        return {"url": self.e.workspace_url(wid)}

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

    def _confirm(self, body: Any) -> dict[str, Any]:
        if self.secrets is None:
            raise SetupError("secrets.not_shown", "show the secrets first", 409)
        if not isinstance(body, dict):
            raise SetupError("invalid.input", "words", 400, field="words")
        out = self.secrets.confirm(body.get("words"))
        if out.get("ok"):
            self.phase = "review"
        elif out["attempts_left"] <= 0:
            # the session dies; a new `orch setup` draws NEW secrets
            self.error = {"code": "secrets.exhausted", "message": "too many wrong words; start the setup again"}
            self.phase = "cancelled"
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
