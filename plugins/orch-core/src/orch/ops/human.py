"""The shared frame of the human operations (C7): who signs, what is checked before the prompt, how an event is
signed and appended.

A person's operation is **never** run for an agent. Three things make that so, each on its own:

* ``ORCH_GRANT`` set means the caller is an agent: the CLI refuses a human operation before any handler runs
  (``human_only``), and :class:`Human` refuses again when a grant reaches it.
* The only thing that signs is the person's device key, held by the passphrase backend (D65): every signature asks for
  the passphrase on ``/dev/tty``, never on stdin, stdout, stderr, an argument or an environment variable. No terminal
  is ``custody.no_prompt``; a wrong passphrase is ``custody.wrong_passphrase`` and writes nothing.
* The person and the device are not named by the caller: the key file for this workspace (``<state dir>/hosts/<workspace
  id>/person/dk.key.json``) has a public key, its device id is looked up in the replayed device roster, and the roster
  says whose device it is. A key the log does not know signs nothing.

Everything the event binds (gate hash, generation, policy hash, source list, ``roster_v``, question hash) is read from
the verified state, never from arguments. The event is judged with the model before the prompt (so a person is not asked
for a passphrase for an event the log will refuse) and judged again by ``Store.append``, which verifies the signature.
The workspace lock is **not** held while the person types.

Test seams: :func:`open_backend` and :func:`show_secret` are module attributes that the tests replace (the passphrase
provider and the scrypt cost are underscore seams of ``PassphraseBackend`` that no production module names). Nothing
here lets a call skip the prompt.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch.custody import Backend, NoPrompt, get_backend
from orch.identity import device_ref, new_ulid, sign_person_event
from orch.ops.base import Context
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, short

__all__ = ["KEY_ID", "Human", "iso", "keys_dir", "open_backend", "show_secret", "source_sha"]

KEY_ID = "dk"
KEY_FILE = KEY_ID + ".key.json"


def keys_dir(state_dir: Path, workspace_id: str) -> Path:
    """Where this person's device key for the workspace lives: next to the workspace key (``keys/wsk``), in a directory
    of its own. One person per workspace per machine in P1 (the real ``orch init``/``keys`` UX is C8)."""
    return state_dir / "hosts" / workspace_id / "person"


def open_backend(directory: Path) -> Backend:
    """The backend over the key directory: the passphrase backend with its terminal prompt and its scrypt floor. Tests
    replace this function; nothing in production passes a prompt callback."""
    return get_backend("passphrase", directory)


def show_secret(text: str) -> None:
    """Write ``text`` to the person's own terminal and nowhere else: not stdout (it may be a pipe, a file or an agent's
    transcript), not stderr, not a file. No terminal is :class:`NoPrompt`."""
    if os.name == "nt":  # pragma: no cover
        raise NoPrompt("no terminal to show the secret on")
    try:
        fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        raise NoPrompt("no controlling terminal to show the secret on") from None
    try:
        if not os.isatty(fd):
            raise NoPrompt("/dev/tty is not a terminal")
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


class _AcceptAll:
    """The verifier of the **pre-check** only: it judges everything but the signatures, which the person has not made
    yet. ``Store.append`` verifies them for real."""

    def verify_person(self, event: Any, context: Any) -> bool:
        return True

    def verify_host(self, event: Any, **kw: Any) -> bool:
        return True

    def verify_embedded(self, event: Any, **kw: Any) -> bool:
        return True


def source_sha(view: Any) -> list[dict[str, str]]:
    """The ticket's current source list as the plain ``source_sha`` value of a decision (sorted by repo already)."""
    return [{"repo": e["repo"], "ref": e["ref"], "sha": e["sha"]} for e in view.source_list]


class Human:
    """One human operation: the person, the backend, the checks, the signature, the append."""

    def __init__(self, ctx: Context, op: str) -> None:
        if ctx.grant is not None or not ctx.human_presence:
            raise OrchError("human_only", "a person's own terminal is needed; an agent never gets this")
        self.ctx = ctx
        self.call = Call.of(ctx, op)
        self.op = op
        self._backend: Backend | None = None
        self._who: tuple[str, str] | None = None

    # -- who
    @property
    def store(self) -> Any:
        return self.call.store

    @property
    def workspace_id(self) -> str:
        return self.store.state.workspace.workspace_id

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            wid = self.workspace_id
            directory = keys_dir(self.call.ws.state_dir, wid)
            if not (directory / KEY_FILE).is_file():  # looked at first: opening the backend would create the directory
                raise OrchError(
                    "not_found",
                    "this machine holds no device key of yours for this workspace",
                    hint="the workspace owner adds you; orch init and keys set up a device",
                )
            self._backend = open_backend(directory)
        return self._backend

    def who(self) -> tuple[str, str]:
        """``(person, device)`` of the key on this machine, from the replayed device roster."""
        if self._who is None:
            device = device_ref(self.backend.public_key(KEY_ID))
            self.store.refresh()
            dv = self.store.state.workspace.devices.get(device)
            if dv is None or dv.removed or dv.revoked:
                raise OrchError("role.denied", "the device key on this machine is not a valid device of a member")
            if dv.person not in self.store.state.workspace.members:
                raise OrchError("role.denied", "the person of this device is no longer a member")
            self._who = (dv.person, device)
        return self._who

    @property
    def person(self) -> str:
        return self.who()[0]

    # -- tickets
    def ticket(self, ref: str | None) -> Any:
        """The view of the ticket ``ref`` (required: a person has no claim), as the signer may see it (§9)."""
        if not ref:
            raise OrchError("ambiguous_ref", "name the ticket REF (--ref DEMO-0043)", hint="orch list")
        part = ref.partition("/")[0]
        view = self.store.ticket(part)
        if view is None:
            raise OrchError("not_found", f"no ticket {short(part, 60)}")
        vis = view.visibility
        if vis != "workspace" and self.person not in vis["restricted"]:
            raise OrchError("not_found", f"no ticket {short(part, 60)}")
        return view

    def gate_basis(self, view: Any, gate: str) -> dict[str, Any]:
        """``gate``, ``gate_gen``, ``hash`` and ``policy_hash`` of the gate as it is now (never from arguments)."""
        g = view.gates[gate]
        if g.hash is None:
            raise OrchError("invalid.input", f"the {gate} gate cannot be hashed right now (see orch show)")
        return {"gate_gen": g.gen, "hash": g.hash, "policy_hash": g.policy_hash}

    def observe(self, view: Any) -> Any:
        """D58: read git now, append the ``branch.pushed`` the ticket is owed, and refuse (``observe.unavailable``) a
        repository that cannot be observed or whose working tree has uncommitted changes: the decision binds a commit,
        not the files a person is looking at. Returns the fresh view."""
        from orch.store import observe

        problems: list[str] = []
        observe.observe(self.store, view.uid, problems)
        view = self.store.ticket(view.uid) or view
        links = view.fields["links"]
        for name in links["repos"]:
            path = observe.repo_path(self.store.root, self.store.state.workspace.repos, name)
            if links["branches"].get(name) and path is not None and path.is_dir():
                dirty = observe.dirty(path)
                if dirty is None:
                    problems.append(f"{name}: git cannot be asked about the working tree")
                elif dirty:
                    problems.append(f"{name}: the working tree has uncommitted changes; the decision binds a commit")
        if problems:
            raise OrchError(
                "observe.unavailable",
                "; ".join(problems)[:200],
                hint="commit or stash, or fix settings.repos or links.branches; orch show says what is seen",
            )
        return view

    # -- checking, signing, appending
    def precheck(self, event: dict[str, Any], log: str) -> None:
        """Judge ``event`` with the model as ``Store.append`` will, without its signature (which does not exist yet).
        Raises what the store would raise, so the person is not asked for a passphrase for a refused event."""
        from orch.model import Refusal, preview
        from orch.store import StoreError

        person, device = self.who()
        self.store.refresh()
        stamp = self.store.peek_stamp(log)
        state = self.store.state
        state = dataclasses.replace(state, _ctx=dataclasses.replace(state._ctx, verifier=_AcceptAll(), admit=True))
        probe = self._envelope(event, log, person, device)
        probe.update(sig="x", seq=stamp["seq"], prev=stamp["prev"], at=stamp["at"])
        if "ws_seq" in stamp:
            probe["ws_seq"] = stamp["ws_seq"]
        r = preview(state, probe, log=log)
        if isinstance(r, Refusal):
            raise StoreError.from_refusal(r)

    def _envelope(self, event: dict[str, Any], log: str, person: str, device: str) -> dict[str, Any]:
        self.store.refresh()
        return {
            "v": 2,
            "id": new_ulid(),
            "actor": {"kind": "person", "id": person, "device": device},
            "auth": self.backend.auth,
            "hash_v": 1,
            "roster_v": self.store.state.workspace.roster_v,
            "based_on": self.store.log_head(log),
            **event,
        }

    def sign_and_append(
        self, event: dict[str, Any], log: str, action: str, *, before_append: Callable[[], None] | None = None
    ) -> Any:
        """Build the person event, ask for the passphrase (the prompt shows the decisive fields of the signing bytes),
        sign, optionally run ``before_append`` (it may refuse: D58 reads git again), append."""
        person, device = self.who()
        ev = self._envelope(event, log, person, device)
        ev["sig"] = sign_person_event(self.backend, KEY_ID, self.workspace_id, log, ev, action=action)
        if before_append is not None:
            before_append()
        return self.store.append(ev, log=log)

    def run(
        self,
        event: dict[str, Any],
        log: str,
        action: str,
        *,
        before_append: Callable[[], None] | None = None,
    ) -> Any:
        """``precheck``, then (unless ``--dry-run``) ``sign_and_append``; ``None`` for a dry run."""
        self.precheck(event, log)
        if self.ctx.dry_run:
            return None
        return self.sign_and_append(event, log, action, before_append=before_append)

    def recheck_source(self, view: Any, signed: list[dict[str, str]]) -> Callable[[], None]:
        """For ``before_append`` of a decision that binds the source list (D58, D59): read git again after the person
        typed the passphrase, and refuse (``gate.stale``) if the commits are no longer the ones that were shown."""

        def check() -> None:
            now = self.observe(view)
            if source_sha(now) != signed:
                raise OrchError(
                    "gate.stale", "the code changed while you were deciding; look at it again", hint="orch show"
                )

        return check

    # -- results
    def ticket_result(
        self, view: Any, done: Any, data: dict[str, Any], hint: str, lines: list[str] | None = None
    ) -> Any:
        fresh = self.store.ticket(view.uid) or view
        seq = done.event["seq"] if done is not None else self.store.head_seq(view.uid)
        return self.call.result(fresh, data, seq=seq, hints=[hint], lines=lines)

    def workspace_result(self, done: Any, data: dict[str, Any], hint: str, lines: list[str] | None = None) -> Any:
        from orch.ops.base import Result

        out = list(lines or [])
        if self.ctx.dry_run:
            out.insert(0, "dry-run: nothing was written")
        seq = done.event["seq"] if done is not None else 0
        return Result(data=data, key=None, seq=seq, hints=[hint], lines=out)

    # -- text
    def text(self, args: dict[str, Any], *, required: bool = False, what: str = "message") -> str | None:
        raw = self.call.read_text_source(args, ("message",))
        if raw is None:
            if required:
                raise OrchError("invalid.input", f"{what} is required: -m TEXT")
            return None
        out = self.call.body_text(raw, what=what)
        return out


def iso(epoch: float) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` of an epoch (whole seconds)."""
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
