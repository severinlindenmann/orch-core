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

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch import canon
from orch.custody import Backend, NoPrompt, get_backend
from orch.identity import device_ref, new_ulid, sign_person_event
from orch.ops import views
from orch.ops.base import Context
from orch.ops.caps import open_tty
from orch.ops.errors import OrchError
from orch.ops.runtime import Call, short
from orch.store.render import thaw

__all__ = [
    "KEY_ID",
    "Human",
    "iso",
    "device_backend",
    "keys_dir",
    "open_backend",
    "review_prompt",
    "secret_destination",
    "show_secret",
    "source_sha",
]

KEY_ID = "dk"
KEY_FILE = KEY_ID + ".key.json"


def keys_dir(state_dir: Path, workspace_id: str) -> Path:
    """Where this person's device key for the workspace lives: next to the workspace key (``keys/wsk``), in a directory
    of its own. One person per workspace per machine in P1 (the real ``orch setup``/``keys`` UX is C8)."""
    return state_dir / "hosts" / workspace_id / "person"


def open_backend(directory: Path) -> Backend:
    """The backend over the key directory: the passphrase backend with its terminal prompt and its scrypt floor. Tests
    replace this function; nothing in production passes a prompt callback."""
    return get_backend("passphrase", directory)


NO_KEY = "this machine holds no device key of yours for this workspace"


def device_backend(state_dir: Path, workspace_id: str, *, convenience: bool = False) -> Backend:
    """The one lookup of this person's device key for a workspace, for every signing path (the CLI's human ops, the
    pairing and genesis-pin commands, the hosted ``Human``). The key is where ``orch setup`` put it (:func:`keys_dir`,
    the passphrase backend) unless the person moved it to convenience custody (``custody.select``), in which case the
    passphrase file is gone and the selection names the backend. Only the hosted path (``convenience=True``: the host
    checks the flag at prepare, bind and submit) may use that backend; direct mode and the genesis pin are
    passphrase-only and refuse a key in one-click custody. Refuses with ``custody.no_key`` when there is none."""
    from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound
    from orch.custody.select import device_backend as resolve
    from orch.custody.select import read_selection

    try:
        sel = read_selection(state_dir, workspace_id)
        if sel is not None and (sel["custody"] == "convenience" or sel["transition"] is not None):
            if not convenience:
                raise OrchError(
                    "custody.no_key",
                    f"{NO_KEY} that this command may use: the key is in one-click custody",
                    hint="use the dashboard or `orch host register`, or `orch keys custody passphrase --in-place`",
                )
            return resolve(state_dir, workspace_id)[0]
    except KeyNotFound:
        raise OrchError("custody.no_key", NO_KEY) from None
    except (BackendUnavailable, CustodyError) as exc:
        raise OrchError("custody.no_key", f"{NO_KEY} ({str(exc)[:200]})") from None
    directory = keys_dir(state_dir, workspace_id)
    if not (directory / KEY_FILE).is_file():  # looked at first: opening the backend would create the directory
        raise OrchError("custody.no_key", NO_KEY)
    return open_backend(directory)


def show_secret(text: str) -> None:
    """Write ``text`` to the person's own terminal and nowhere else: not stdout (it may be a pipe, a file or an agent's
    transcript), not stderr, not a file. No terminal is :class:`NoPrompt`."""
    if os.name == "nt":  # pragma: no cover
        raise NoPrompt("no terminal to show the secret on")
    try:
        fd = open_tty(os.O_WRONLY)
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

    def genesis_failure(self, event: Any) -> str | None:
        return None

    def verify_hub_owner_proof(self, event: Any, **kw: Any) -> str | None:
        return None  # #1198 C9: the owner proof is made after the review; the submit and the append check it for real


def review_prompt(text: str, expect: str) -> bool:
    """Show ``text`` on the person's own terminal and go on only if the person types ``expect`` (the ticket key) and
    Enter after reading it. ``/dev/tty`` only (like the passphrase), so an agent holding stdin and stdout sees none of
    it and cannot answer; no terminal is :class:`NoPrompt`. Anything else (another word, end of input, a line cut off
    by end of input, Ctrl-C) is ``False``: nothing is signed. No flag or variable skips it. Tests replace this."""
    if os.name == "nt":  # pragma: no cover
        raise NoPrompt("no terminal to show the review on")
    try:
        fd = open_tty(os.O_RDWR)
    except OSError:
        raise NoPrompt("no controlling terminal to show the review on") from None
    try:
        if not os.isatty(fd):
            raise NoPrompt("/dev/tty is not a terminal")
        details = getattr(text, "details", "") or ""  # B6: a plain view with the full review behind `d`
        if not details:
            prompt = f"Type {expect} and Enter to continue; anything else stops: "
        else:
            prompt = f"Type {expect} to continue (d = details): "
        shown = text
        if details and getattr(text, "details_first", False):  # --details: the full review first, no `d` needed
            shown, details = details, ""
        os.write(fd, ("\n=== orch: read before you sign ===\n" + shown + prompt).encode())
        b6_view = bool(details)  # only a B6 review (plain view, full one behind d)
        seen_details = bool(getattr(text, "details", "")) and not details  # --details: the full review came first
        import termios

        while True:
            termios.tcflush(fd, termios.TCIFLUSH)  # whatever was typed before the content appeared confirms nothing
            line = bytearray()
            try:
                while True:
                    c = os.read(fd, 1)
                    if not c:
                        return False  # end of input: no newline came
                    if c == b"\n":
                        break
                    line += c
            except (KeyboardInterrupt, OSError):
                return False
            answer = bytes(line).strip().decode("utf-8", "replace")
            if details and answer == "d":  # the full review, then the same question
                os.write(fd, ("\n=== details ===\n" + details + prompt).encode())
                details = ""  # shown once; a second `d` is an answer that stops
                seen_details = True
                continue
            if answer == expect and b6_view:
                from orch.custody.passphrase import note_plain_review

                note_plain_review(
                    hint=not seen_details
                )  # one-shot: the next passphrase prompt only, and only after a confirmed review
            return answer == expect
    finally:
        os.close(fd)


def secret_destination(prompt: str) -> str:
    """``orch sign`` of a runner grant (H11c): ``r`` hands the secret to the orch runner, ``s`` shows it here. Asked on
    the person's own terminal only (``/dev/tty``); anything but ``r`` is ``s``. Tests replace this."""
    if os.name == "nt":  # pragma: no cover
        raise NoPrompt("no terminal to ask on")
    try:
        fd = open_tty(os.O_RDWR)
    except OSError:
        raise NoPrompt("no controlling terminal to ask on") from None
    try:
        if not os.isatty(fd):
            raise NoPrompt("/dev/tty is not a terminal")
        os.write(fd, prompt.encode())
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        line = bytearray()
        while True:
            c = os.read(fd, 1)
            if not c or c == b"\n":
                break
            line += c
        return "r" if bytes(line).strip().lower() == b"r" else "s"
    except (KeyboardInterrupt, OSError):
        return "s"
    finally:
        os.close(fd)


def source_sha(view: Any) -> list[dict[str, str]]:
    """The ticket's current source list as the plain ``source_sha`` value of a decision (sorted by repo already)."""
    return [{"repo": e["repo"], "ref": e["ref"], "sha": e["sha"]} for e in view.source_list]


class Human:
    """One human operation: the person, the backend, the checks, the signature, the append."""

    def __init__(self, ctx: Context, op: str) -> None:
        if ctx.grant is not None or (not ctx.human_presence and ctx.prepare is None):
            raise OrchError("human_only", "a person's own terminal is needed; an agent never gets this")
        if ctx.prepare is None:  # human.prepare (H5a) signs nothing here: the signing client does
            ctx.caps.signer.require()
        self.ctx = ctx
        self.call = Call.of(ctx, op)
        self.op = op
        self._backend: Backend | None = None
        self._who: tuple[str, str] | None = None
        #: H12d-2: readers this request admits (direct mode; the host keeps them in ``Capture.readers``)
        self.readers: list[dict[str, Any]] = []

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
            self._backend = device_backend(self.call.ws.state_dir, wid)
        return self._backend

    @property
    def auth(self) -> str:
        """The ``auth`` of the events: the backend's, or (``human.prepare``) the bound signer's."""
        capture = self.ctx.prepare
        return capture.probe_auth if capture is not None else self.backend.auth  # type: ignore[return-value]

    def who(self) -> tuple[str, str]:
        """``(person, device)`` of the key on this machine, from the replayed device roster. Under ``human.prepare``
        the person the host mapped, judged under one of their valid devices (:class:`orch.ops.prepare.Capture`)."""
        capture = self.ctx.prepare
        if capture is not None:
            return capture.person, capture.probe_device
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

    def _direct_registrations(self, git: Any) -> Any:
        """Direct mode (D-P2-38): ``git`` with the workspace's worktree registrations (``git-pins.json`` next to the
        host's state of this workspace, written by ``orch worktree``), when the file exists."""
        import dataclasses

        from orch.gitread.client import PinStore

        path = self.call.ws.state_dir / "hosts" / self.store.workspace_id / "git-pins.json"
        if not path.exists():
            return git
        return dataclasses.replace(git, worktrees=PinStore(path))

    def _direct_binding(self, git: Any, view: Any, name: str, copy: Any) -> str | None:
        """Direct mode (D58, D-P2-38): the first look records the working copy's binding (generation, registration,
        pin); every later look (the precommit, under the workspace lock) must find the same one, so a worktree removed
        or recreated while the person was deciding is refused, never read in its new or the declared place. Without a
        pin file, a worktree directory on disk at the ticket's path is refused too (Opus #439 F3): the file was
        deleted, or the worktree was made by hand."""
        binding = copy.binding(view.uid, name)
        bound: dict[str, Any] = self.__dict__.setdefault("_direct_bound", {})
        key = (view.uid, name)
        if key in bound and bound[key] != binding:
            return f"{name}: the ticket's worktree registration changed while you were deciding; look again"
        bound.setdefault(key, binding)
        if git.worktrees is None and not copy.registered:
            from orch.host.worktrees import worktree_path

            declared = git.repo_path(self.store.layout, self.store.state.workspace.repos, name)
            if declared is not None and os.path.lexists(worktree_path(os.fspath(declared), self.store.workspace_id,
                                                                      view.key)):  # fmt: skip
                return f"{name}: the ticket has a worktree on disk but no registration (git-pins.json is missing)"
        return None

    def _tree_problems(self, view: Any, signed: list[dict[str, str]]) -> list[str]:
        """Read-only: for each linked repository with a branch, is the working copy **on the bound branch, at the bound
        commit, with nothing uncommitted**? The decision binds a commit; a person who ran or read the files must have
        had exactly that commit in front of them (D58, security review R2). ``signed`` is the source list bound."""
        out: list[str] = []
        links = view.fields["links"]
        checked: set[str] = set()  # identities of the working copies looked at, each against the signed list
        for name in links["repos"]:
            branch = links["branches"].get(name)
            if not branch:
                continue
            git = self.ctx.caps.git
            if not git.enabled:  # git denied (an unreadable pin file in host mode): refused, never an internal error
                out.append(f"{name}: git cannot be read here")
                continue
            # H11d: the ticket's registered worktree when it has one (never a fallback), else the declared copy
            if git.reader is None and git.worktrees is None:  # direct mode: the registrations `orch worktree` wrote
                try:
                    git = self._direct_registrations(git)
                except (ValueError, OSError) as exc:  # a pin file that cannot be read: fail closed
                    out.append(f"{name}: the worktree registrations cannot be read ({exc})")
                    continue
            copy = git.ticket_copy(self.store.layout, self.store.state.workspace.repos, view.uid, name)
            path = copy.path
            if self.ctx.prepare is None and git.reader is None:
                # direct mode (Codex #439): the copy the person was shown is the one the precommit reads
                problem = self._direct_binding(git, view, name, copy)
                if problem is not None:
                    out.append(problem)
                    continue
            if copy.why is not None:
                out.append(copy.why)
                continue
            if path is None or not path.is_dir():
                where = "the ticket's worktree is missing" if copy.registered else "no working copy in settings.repos"
                out.append(f"{name}: {where}")
                continue
            reg_pins = None  # direct mode, a registered worktree: read exactly its registered pins (#583, L4)
            if copy.registered and git.reader is None:  # direct mode: the pin must still hold (host mode: bound_copy)
                from orch.gitread.errors import Unavailable
                from orch.gitread.fs import RepoPins
                from orch.gitread.pinning import verify_worktree_pins

                reg = git.worktrees.worktree(view.uid, name)["reg"] if git.worktrees is not None else None
                try:
                    if reg is None or reg.get("pins") is None:
                        raise Unavailable("the worktree registration has no pins")
                    reg_pins = RepoPins.from_json(reg["pins"])
                    verify_worktree_pins(os.fspath(path), reg_pins)
                except Unavailable as exc:
                    out.append(f"{name}: {exc.reason}"[:300])
                    continue
                # no git gate here any more (#565): direct mode reads the worktree with orch's own reader, which runs
                # no program and refuses a populated submodule itself (unknown, never clean)
            if self.ctx.prepare is not None:
                self.ctx.prepare.copies.append(copy.binding(view.uid, name))
            ref = f"refs/heads/{branch}"
            state = why = None
            if git.reader is None:
                # direct mode (#583, L4): HEAD, its branch and the tree from ONE read of the reader (as host mode's
                # tree.check), so "clean" is clean at the commit compared with the signed one
                from orch.store.observe import tree_state

                state, why = tree_state(path, pins=reg_pins)
                head = state["head"] if state is not None else None
                on = state["head_ref"] if state is not None else None
            else:
                head = git.head(path)
                on = git.symbolic_head(path)
            identity = git.identity(path, name)
            checked.add(identity)
            entry = next((e for e in signed if e["repo"] == identity), None)
            if head is None or entry is None or entry["ref"] != ref or entry["sha"] != head:
                out.append(f"{name}: {ref} is not recorded at the commit checked out; look again with orch show")
            elif on != ref:
                out.append(f"{name}: the working tree is on {on or 'a detached commit'}, the decision binds {ref}")
            if git.reader is None:  # direct mode: the reader in-process (no git process, #565)
                if state is None:
                    out.append((f"{name}: the working tree cannot be read" + (f": {why}" if why else ""))[:300])
                elif state["dirty"]:
                    out.append(f"{name}: the working tree has uncommitted changes; the decision binds a commit")
                continue
            out += self._tree_check(git, path, name, entry, ref)
        for e in signed:  # every repo the decision binds must have been checked, not only the linked ones
            if e["repo"] not in checked:
                out.append(f"{e['repo']}: in the source list but no working copy of it was checked")
        return out

    def _tree_check(self, git: Any, path: Any, name: str, entry: dict[str, str] | None, ref: str) -> list[str]:
        """Host mode: the ``tree.check`` job (N3). Clean only at the commit being signed: the child reads ``HEAD`` and
        the tree in one call, and its ``head`` and ``head_ref`` must equal the signed entry. A dry run runs no
        ``tree.check`` job (section 0.8.1) and records no ``checks``."""
        capture = self.ctx.prepare
        if capture is not None and capture.dry_run:
            return []
        got = git.tree_check(path, name)
        if got is None:
            return [f"{name}: git cannot be asked about the working tree"]
        if entry is None or got["head"] != entry["sha"] or got["head_ref"] != ref:
            return [f"{name}: the working tree is not at the commit the decision binds; look again with orch show"]
        if got["changed"] or got["more"]:
            return [f"{name}: the working tree has uncommitted changes; the decision binds a commit"]
        if capture is not None:  # human.prepare: the unsigned checks of N3
            from orch.gitread.treejob import check_entry

            capture.checks.append(check_entry(name, self.ctx.caps.clock()))
        return []

    def observe(self, view: Any) -> Any:
        """D58: read git now and append the ``branch.pushed`` the ticket is owed (never under ``--dry-run``, which
        writes nothing), then refuse (``observe.unavailable``) unless each linked working copy is on the bound branch at
        the bound commit and clean. Returns the fresh view."""
        problems: list[str] = []
        if not self.ctx.dry_run and self.ctx.prepare is None:  # prepare appends nothing (the host's branch.pushed too)
            self.ctx.caps.git.observe(self.store, view.uid, problems)
            view = self.store.ticket(view.uid) or view
        problems += self._tree_problems(view, source_sha(view))
        if problems:
            raise OrchError(
                "observe.unavailable",
                "; ".join(dict.fromkeys(problems))[:200],
                hint="check out the branch at its tip and commit or stash, or fix settings.repos or links.branches",
            )
        return view

    # -- what the person reads before signing
    def artifacts_ok(self, view: Any, gate: str) -> None:
        """F1 5.7: recompute the SHA-256 of every bound artifact file; a mismatch is ``artifact.mismatch``."""
        g = view.gates[gate].input or {}
        self._checked_artifacts = {}
        for name, a in (g.get("artifacts") or {}).items():
            p = self.store.root / "tickets" / view.uid / "artifacts" / name
            try:
                if p.is_symlink():
                    raise OSError("symlink")
                data = self.ctx.caps.files.read_bytes(p)
            except OSError:
                raise OrchError("artifact.mismatch", f"{short(name, 60)}: the file cannot be read") from None
            if canon.artifact_digest(data) != a["digest"]:
                raise OrchError("artifact.mismatch", f"{short(name, 60)} differs from the digest in the log")
            self._checked_artifacts[(view.uid, name, a["digest"])] = data

    def header(self, view: Any, *more: str) -> list[str]:
        """What the person reads before any ticket operation: which ticket (the key is derived by orch from the log id
        that the signature names), then ``more`` (ticket text: escaped and marked)."""
        out = [f"ticket: {view.key} (derived by orch from the log id below, not signed)", f"log id: {view.uid}"]
        out += ["| " + canon.clean(x) for m in more for x in m.split("\n")]
        return [*out, ""]

    def review(self, view: Any, gate: str) -> list[str]:
        """The gate's bound content, as the gate hash binds it (F1 5.7): the section texts (each checked against its
        hash), acceptance criteria, tasks with their verify commands, links, the source list and the artifact digests;
        then a short gate hash, the one ``orch show`` prints. Ticket text is data: every line is escaped and marked."""
        g = view.gates[gate]
        inp = g.input or {}
        texts = self.store.body_sections(view.uid)
        out = [*self.header(view)[:-1], f"gate: {gate} generation {g.gen}"]
        f = inp.get("fields") or {}
        # orch's own words (headings, labels) are printed bare; only ticket text, which an agent wrote, gets "| ", so
        # text cannot pass for a heading
        body: list[str] = []
        counts: list[str] = []
        for sid, h in (inp.get("sections") or {}).items():
            text = texts.get(sid, "")
            if canon.section_hash(text) != h:
                raise OrchError("gate.stale", f"section {sid} changed while the review was built", hint="orch show")
            lines = text.split("\n") if text else []
            counts.append(f"{sid}: {len(lines)} lines")
            body += [f"--- section {sid} ({len(lines)} lines) ---", *self._data(lines or ["(empty)"])]
        body.append(f"--- type {f.get('ticket_type')} size {f.get('size')} ---")
        acceptance = f.get("acceptance") or []
        for a in acceptance:
            body += [f"{a['id']}:", *self._data(a["text"].split("\n"))]
            if gate == "verify":
                from orch.host.acceptance import artifact_statement
                from orch.model.accept import criterion_hash

                ticket = self.store.state._core.tickets[view.uid]
                for art in ticket.artifacts.values():
                    data = getattr(self, "_checked_artifacts", {}).get((view.uid, art.name, art.digest))
                    if art.ac != a["id"] or data is None:
                        continue
                    statement = artifact_statement(art, data)
                    if statement is None:
                        continue
                    if statement["hash"] == criterion_hash(a["id"], a["text"]):
                        body.append(f"{a['id']}: accepted manually by {art.by} (no automated check)")
                    else:
                        body.append(f"{a['id']}: accepted for an earlier wording, no longer counts")
        tasks = inp.get("tasks") or []
        for t in tasks:
            verify = (t.get("verify") or {}).get("cmd")
            body += [f"{t['id']}:", *self._data(t["text"].split("\n")), "   verify:", *self._data([verify or "(none)"])]
            body.append(f"   proves: {','.join(t.get('proves') or []) or '-'}")
        if f.get("links") is not None:
            body += ["links:", *self._data([canon.dumps(thaw(f["links"])).decode()])]
        for e in inp.get("source_sha") or []:
            body += ["source:", *self._data([f"{e['repo']} {e['ref']} {e['sha']}"])]
        for name, a in (inp.get("artifacts") or {}).items():
            body += ["artifact:", *self._data([f"{name} kind={a.get('kind')} {a['digest']}"])]
        for tid, r in (inp.get("receipts") or {}).items():
            body += [
                f"receipt {tid}:",
                *self._data([f"exit {r.get('exit')} at {r.get('commit')} repo {r.get('repo')}"]),
                "   (orch's label: attested by the agent, not run by orch or the host)",
            ]
        summary = ", ".join([*counts, f"{len(acceptance)} criteria", f"{len(tasks)} tasks"])
        out += [f"summary: {summary}", "", *body]
        out += ["", "also bound by the gate hash, not shown here: addon fields and packages, the policy and people"]
        out += ["lists, and the approvals of earlier gates (the passphrase prompt shows the policy hash)"]
        out += ["", f"gate hash {views.short_gate_hash(g.hash)} (orch show prints the same)", ""]
        return out

    @staticmethod
    def _data(lines: list[str]) -> list[str]:
        """Ticket text, escaped, one ``| `` mark per line."""
        return ["| " + canon.clean(x) for x in lines for x in x.split("\n")]

    def refuse_repeat(self, view: Any, gate: str) -> None:
        """A person counts once per gate generation (``generations.counting``): a second approval changes nothing."""
        g = view.gates[gate]
        me = self.person
        if any(
            d.person == me and d.gen == g.gen and d.kind in ("approve", "pass") and not d.voided for d in g.decisions
        ):
            raise OrchError("gate.already_approved", f"you already approved {gate} of {view.key} at this generation")

    # -- readers (H12d-2, §0.8.6 item 9.4)
    def admit_reader(self, entry: dict[str, Any]) -> None:
        """A reader this request adds, with its verified declaration (``{device, person, decl, source, reader,
        level, digest}``): the host's ``Capture`` keeps it for the request, direct mode for its own append."""
        capture = self.ctx.prepare
        (capture.readers if capture is not None else self.readers).append(entry)

    def _readers_check(self, inner: Callable[[], None] | None, events: list[dict[str, Any]]) -> Callable[[], None]:
        """``precommit`` of **direct mode's append choke point** (§0.8.6 item 9.4): under the store lock, right before
        the append, :func:`orch.ops.readers.check_readers` over the current levels plus this request's readers, then
        their records are written (a record that cannot be written refuses the append)."""
        from orch.ops import readers as rd
        from orch.store.readerrecs import relay_level

        state_dir, wid, pending = self.call.ws.state_dir, self.workspace_id, list(self.readers)

        def check() -> None:
            if inner is not None:
                inner()
            self.store.refresh()
            ws = self.store.state.workspace
            lv = rd.levels(state_dir, wid, self.store.state)
            lv.update({p["device"]: p["level"] for p in pending})
            required = rd.required_reader(self.store, state_dir, wid) if rd.added_readers(events, ws) else 0
            try:
                rd.check_readers(ws, lv, events, required=required, relay_level=relay_level(state_dir, wid))
            except rd.ReadersRefused as exc:
                raise exc.orch_error() from None
            stamp = iso(self.ctx.now())
            try:  # every added device starts clean: only the declaration admitted here may count (#513 review)
                rd.drop_stale(state_dir, wid, rd.added_readers(events))
            except Exception as exc:  # noqa: BLE001 - a stale record that stays would count again: refuse
                raise OrchError(
                    "internal",
                    f"a stale reader record could not be removed ({type(exc).__name__}); nothing was appended",
                    hint="check the state directory: orch doctor",
                ) from None
            for p in pending:
                try:
                    rd.store_record(
                        state_dir,
                        wid,
                        os.getuid(),
                        rd.make_record(_Declared(p), p["decl"], p["person"], p["source"], stamp),
                    )
                except Exception as exc:  # noqa: BLE001 - any doubt refuses the append
                    raise OrchError(
                        "internal",
                        f"the reader record could not be written ({type(exc).__name__}); nothing was appended",
                        hint="check the state directory: orch doctor",
                    ) from None

        return check

    # -- checking, signing, appending
    def precheck(self, event: dict[str, Any], log: str) -> None:
        """Judge ``event`` with the model as ``Store.append`` will, without its signature (which does not exist yet).
        Raises what the store would raise, so the person is not asked for a passphrase for a refused event."""
        from orch.model import Refusal, preview
        from orch.store import StoreError

        person, device = self.who()
        self.store.refresh()
        stamp = self.store.peek_stamp(log)
        from orch.ops.prepare import accepting

        state = accepting(self.store.state)
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
            "auth": self.auth,
            "hash_v": 1,
            "roster_v": self.store.state.workspace.roster_v,
            "based_on": self.store.log_head(log),
            **event,
        }

    def _sign_and_append(
        self,
        event: dict[str, Any],
        log: str,
        action: str,
        precommit: Callable[[], None] | None = None,
        artifacts: dict[str, bytes] | None = None,
    ) -> Any:
        """Build the person event, ask for the passphrase (the prompt shows the decisive fields of the signing bytes),
        sign, append; ``precommit`` runs inside ``Store.append`` with the workspace lock held (D58: git is read again
        there, so nothing can move between the check and the append)."""
        person, device = self.who()
        ev = self._envelope(event, log, person, device)
        ev["sig"] = sign_person_event(self.backend, KEY_ID, self.workspace_id, log, ev, action=action)
        from orch.ops.readers import READER_EVENTS

        typ = ev.get("type")
        if self.readers or typ in READER_EVENTS or typ == "restore" or ev.get("auth") == "convenience":
            precommit = self._readers_check(precommit, [ev])  # H12d-2: before the mark, so a refusal sets none
        if ev.get("auth") == "convenience":  # #441: the durable mark, inside the append, before the event is written
            precommit = self._mark_seen(precommit)
        return self.store.append(ev, log=log, precommit=precommit, artifacts=artifacts)

    def _mark_seen(self, inner: Callable[[], None] | None) -> Callable[[], None]:
        """``precommit`` that also sets the durable ``convenience_seen`` mark (:mod:`orch.ops.readers`); a mark that
        cannot be written refuses the append (fail closed)."""
        from orch.ops.readers import ensure_convenience_mark

        state_dir, wid = self.call.ws.state_dir, self.workspace_id

        def check() -> None:
            if inner is not None:
                inner()
            try:
                ensure_convenience_mark(state_dir, wid, os.getuid())
            except Exception as exc:  # noqa: BLE001 - any doubt refuses the append
                raise OrchError(  # a global code: any person op may sign with convenience custody
                    "internal",
                    f"the convenience_seen mark could not be written ({type(exc).__name__}); nothing was appended",
                    hint="check the state directory: orch doctor",
                ) from None

        return check

    def run(
        self,
        event: dict[str, Any],
        log: str,
        action: str,
        *,
        precommit: Callable[[], None] | None = None,
        review: list[str] | None = None,
        artifacts: dict[str, bytes] | None = None,
    ) -> Any:
        """``precheck``, then (unless ``--dry-run``) show ``review`` on the terminal and ask the person to confirm, sign
        and append; ``None`` for a dry run. The word the person types comes from what is signed: the verified key of
        the ticket log, or the ``name`` of a workspace event (``addon.granted``); no such word is a refusal."""
        capture = self.ctx.prepare
        grant_probe = capture is not None and event.get("type") == "grant.issued" and event.get("secret_hash") is None
        # a prepared grant has no secret_hash (the signer draws the secret, F1): the model judges it with a placeholder
        self.precheck({**event, "secret_hash": "sha256:" + "0" * 64} if grant_probe else event, log)
        if self.ctx.dry_run:
            return None
        if capture is not None:
            from orch.ops.prepare import Captured

            capture.add(Captured(log, dict(event), artifacts=artifacts))  # submit re-reads git itself (D58)
            return None
        if review is not None:
            if log == "workspace":  # no ticket key: the name the event itself carries, never a word the caller picks
                name = event.get("name")
                word = name if isinstance(name, str) and name else None
            else:
                view = self.store.ticket(log)  # the key the person types is the verified key of the log being signed
                word = view.key if view is not None else None
            if word is None or not review_prompt("\n".join(review), word):
                raise OrchError("invalid.input", "not confirmed: nothing was signed")
        return self._sign_and_append(event, log, action, precommit, artifacts)

    def recheck_source(self, view: Any, signed: list[dict[str, str]]) -> Callable[[], None]:
        """For ``precommit`` of a decision that binds the source list (D58, D59): inside the append, with the lock held,
        read git again (read-only) and refuse (``gate.stale``) unless it is still the commits that were shown."""

        def check() -> None:
            if self._tree_problems(view, signed):
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

    def workspace_result(
        self, done: Any, data: dict[str, Any], hint: str | None, lines: list[str] | None = None
    ) -> Any:
        from orch.ops.base import Result

        out = list(lines or [])
        if self.ctx.dry_run:
            out.insert(0, "dry-run: nothing was written")
        seq = done.event["seq"] if done is not None else 0
        return Result(data=data, key=None, seq=seq, hints=[hint] if hint else [], lines=out)

    # -- text
    def text(self, args: dict[str, Any], *, required: bool = False, what: str = "message") -> str | None:
        raw = self.call.read_text_source(args, ("message",))
        if raw is None:
            if required:
                raise OrchError("invalid.input", f"{what} is required: -m TEXT")
            return None
        out = self.call.body_text(raw, what=what)
        return out


class _Declared:
    """The ``Declared`` fields of a pending reader entry (for :func:`orch.ops.readers.make_record`)."""

    def __init__(self, entry: dict[str, Any]) -> None:
        self.device_id = entry["device"][2:]
        self.reader, self.level, self.digest = entry["reader"], entry["level"], entry["digest"]


def iso(epoch: float) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` of an epoch (whole seconds)."""
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
