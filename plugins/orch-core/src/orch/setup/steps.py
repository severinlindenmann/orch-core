"""The pieces of the setup run: the secrets of one session, and the create sequence (:class:`Creator`).

The create sequence (batch-9.md "## Design" 3):

1. (R3) the setup terminal shows the fingerprint words of the review and waits for the person to type yes when
   the page shows the same words, when the review asks;
2. the keys and the genesis through the B5 bootstrap (``host.wsk_create`` + ``host.bootstrap``, one intent, rollback of
   the recorded entries only); nothing is durable before this;
3. the add-on manifests are written into the workspace, the runner/execution flags too, and a runner grant waits for
   the runner's hello (R4) or is skipped (never sign a grant that cannot be deposited);
4. ONE unlock signs, in order, ``settings.changed``, ``addon.granted`` x N and ``grant.issued`` last: every request is
   prepared and checked against the review (R1) BEFORE the first signature. ``human.prepare`` is one operation per
   request (no dependent bundle), so this is the written fallback: prepare all, check all, unlock once, sign and submit
   in order, deposit the grant in-process;
5. the convenience flag (one-click), then the custody conversion LAST (R6).

Everything the host does is behind :class:`Ports`, so the sequence is tested with a fake and wired to the real host by
:class:`RealPorts`.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import secrets as _secrets
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orch.custody.base import CustodyError
from orch.layout import STORE_DIR, WORK_DIR

from . import review as review_mod
from .protocol import SetupError

__all__ = [
    "CONFIRM_ATTEMPTS",
    "Creator",
    "Genesis",
    "Ports",
    "RealPorts",
    "Run",
    "Secrets",
    "set_no_core_dumps",
]

CONFIRM_ATTEMPTS = 5
CODE_WORDS = 24
#: what the person types on the setup terminal once the page shows the same three words (R3)
TTY_WORD = "yes"


def tty_confirm_text(words: str) -> str:
    """The block the setup terminal shows before it asks for :data:`TTY_WORD` (plain words, owner B11)."""
    return (
        "\n========================================================\n"
        "orch setup is asking you to confirm.\n"
        "The setup page in your browser should show these three words:\n\n"
        f"    {words}\n\n"
        f"Check the page. If they match, type {TTY_WORD} and press Enter.\n"
        "Anything else (or Enter alone) cancels; nothing is created.\n"
        "========================================================\n"
    )


CHECKS = 3


def batch_labels() -> tuple[bytes, ...]:
    """The domain labels the one unlock may sign: the setup batch is WORKSPACE-log person events only
    (``settings.changed``, ``addon.granted``, ``grant.issued``: ``sig_ws_event``), never a ticket's."""
    from orch import canon

    return (canon.LABELS["sig_ws_event"].encode("ascii"),)


def set_no_core_dumps() -> None:
    """RLIMIT_CORE=0: a crash of the setup process leaves no core file holding the passphrase or the recovery code."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):  # pragma: no cover - not available here: nothing more can be done
        pass


def _wipe(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


class Secrets:
    """The device passphrase and the recovery code of ONE session, generated in this process, held in bytearrays and
    wiped when the session ends. ``reveal`` works once (a second call is ``secrets.shown``, 410); ``confirm`` checks
    three words in core; five wrong tries end the session and the next session draws new secrets.

    Python cannot promise that no copy of a ``str`` survives (the custody API takes ``str``); the copies made by
    :meth:`passphrase` and :meth:`code` are as short-lived as the caller makes them. No secret is ever in a log, an
    error or a state file."""

    def __init__(self, *, show_passphrase: bool) -> None:
        from orch.custody.strength import generate_passphrase
        from orch.identity import generate_recovery_code

        self._lock = threading.Lock()
        self.show_passphrase = show_passphrase
        self._pass = bytearray(generate_passphrase().encode("ascii"))
        self._code = bytearray(generate_recovery_code().encode("ascii"))
        self.positions = sorted(_secrets.SystemRandom().sample(range(1, CODE_WORDS + 1), CHECKS))
        self.shown = False
        self.confirmed = False
        self.attempts_left = CONFIRM_ATTEMPTS
        self.dead = False

    # -- the browser's two calls
    def reveal(self) -> dict[str, Any]:
        with self._lock:
            if self.dead:
                raise SetupError("secrets.gone", "these secrets are gone; start the setup again", 410)
            if self.shown:
                raise SetupError("secrets.shown", "the secrets were shown once and are not shown again", 410)
            self.shown = True
            words = bytes(self._code).decode("ascii").split()
            return {
                "passphrase": bytes(self._pass).decode("ascii") if self.show_passphrase else None,
                "recovery_words": words,
                "check_positions": list(self.positions),
            }

    def confirm(self, given: Any) -> dict[str, Any]:
        with self._lock:
            if self.dead:
                raise SetupError("secrets.gone", "these secrets are gone; start the setup again", 410)
            if not self.shown:
                raise SetupError("secrets.not_shown", "show the secrets first", 409)
            if self.confirmed:
                return {"ok": True}
            if not isinstance(given, dict) or set(given) != {str(p) for p in self.positions}:
                raise SetupError("invalid.input", "words are the three asked positions", field="words")
            words = bytes(self._code).decode("ascii").split()
            ok = True
            for p in self.positions:  # all three are compared, whatever the first said
                w = given[str(p)]
                ok &= isinstance(w, str) and w.strip().lower() == words[p - 1]
            del words
            if ok:
                self.confirmed = True
                return {"ok": True}
            self.attempts_left -= 1
            if self.attempts_left <= 0:
                self._wipe()
            return {"ok": False, "attempts_left": self.attempts_left}

    # -- the create step's
    def passphrase(self) -> str:
        with self._lock:
            if self.dead or not self.confirmed:
                raise SetupError("secrets.not_confirmed", "the secrets were not confirmed", 409)
            return bytes(self._pass).decode("ascii")

    def code(self) -> str:
        with self._lock:
            if self.dead or not self.confirmed:
                raise SetupError("secrets.not_confirmed", "the secrets were not confirmed", 409)
            return bytes(self._code).decode("ascii")

    def held(self) -> list[str]:
        """Every secret this session holds, for :func:`redact_reason` only (never shown, logged or stored)."""
        with self._lock:
            if self.dead:
                return []
            code = bytes(self._code).decode("ascii")
            return [bytes(self._pass).decode("ascii"), code, *code.split()]

    def _wipe(self) -> None:
        _wipe(self._pass)
        _wipe(self._code)
        self.dead = True

    def wipe(self) -> None:
        with self._lock:
            self._wipe()

    @property
    def exhausted(self) -> bool:
        return self.dead and self.attempts_left <= 0

    def __repr__(self) -> str:  # never the secrets
        return "Secrets(<redacted>)"


# ============================================================================================ redacting error text

HIDDEN_REASON = "details hidden because they contained a secret; see the terminal"
_MIN_SECRET = 6  # a shorter "secret" would match ordinary words


def _forms(text: str) -> tuple[str, str]:
    """Two normal forms of ``text``: lower-cased with ALL whitespace removed, and lower-cased letters/digits only.
    Splitting a secret by spaces, newlines or tabs (or joining its words with punctuation) changes neither."""
    low = text.lower()
    return "".join(low.split()), "".join(c for c in low if c.isalnum())


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    for c in text.lower():
        if c.isalnum():
            cur.append(c)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def _b64_forms(b: bytes) -> list[str]:
    """Base64 (standard and URL-safe, no padding) of ``b`` as it would appear at any of the 3 byte alignments inside a
    longer encoded text; the unstable first/last characters are dropped."""
    out: list[str] = []
    for k in range(3):
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            e = enc(b"\0" * k + b).decode("ascii").rstrip("=")
            skip = (k * 8 + 5) // 6 + (1 if k else 0)
            e = e[skip : len(e) - 2 if len(e) > 8 else len(e)]
            if len(e) >= _MIN_SECRET:
                out.append(e)
    return out


def contains_secret(text: str, secrets: Iterable[str]) -> bool:
    """True when ``text`` holds a secret. For each secret (the passphrase, the recovery code, each recovery word):

    * the whole secret, or any run of its words, in whitespace-removed or letters/digits-only lower-case form;
    * any single word of it of at least 6 characters (so a single recovery word of 6+ letters triggers), and any two
      consecutive words joined, as whole tokens or run together;
    * its hex, base64 (standard / URL-safe, any alignment) and URL-encoded (%20, +) forms; URL-encoded text is also
      decoded before the comparison.

    Fails closed on odd input: a non-str secret is ignored, never raised on."""
    texts = {text, urllib.parse.unquote(text), urllib.parse.unquote_plus(text)}
    forms = [_forms(t) for t in texts]
    toks = [set(_tokens(t)) for t in texts]
    raw_nows = ["".join(t.split()) for t in texts]
    for sec in secrets:
        if not isinstance(sec, str) or not sec.strip():
            continue
        s_ws, s_an = _forms(sec)
        for t_ws, t_an in forms:
            if len(s_ws) >= _MIN_SECRET and s_ws in t_ws:
                return True
            if len(s_an) >= _MIN_SECRET and s_an in t_an:
                return True
        words = _tokens(sec)
        for w in words:
            if len(w) >= _MIN_SECRET and any(w in tk for tk in toks):
                return True
        for w1, w2 in zip(words, words[1:], strict=False):
            pair = w1 + w2
            if len(pair) >= _MIN_SECRET and any(pair in t_an for _, t_an in forms):
                return True
        raw = sec.encode("utf-8", "replace")
        hexed = {raw.hex(), "".join(sec.split()).encode("utf-8", "replace").hex()}
        if any(h in t_ws for h in hexed for t_ws, _ in forms):
            return True
        for variant in (sec, " ".join(sec.split()), "".join(sec.split())):
            vb = variant.encode("utf-8", "replace")
            for e in _b64_forms(vb):
                if any(e in r for r in raw_nows):
                    return True
            for q in (urllib.parse.quote(variant), urllib.parse.quote_plus(variant)):
                if len(q) >= _MIN_SECRET and any(q.lower() in t_ws for t_ws, _ in forms):
                    return True
    return False


#: The fixed sentences (and pieces) this flow's own code writes into reasons. A secret word that occurs only inside one
#: of these is not a leak (a recovery word "use" must not hide "macOS did not let orch use the keychain"). Closed list,
#: matched exactly, case-sensitively and on whole-word edges, longest first; add only verbatim sentences.
KNOWN_WORDING = tuple(
    sorted(
        (
            HIDDEN_REASON,
            "macOS did not let orch use the keychain (it is locked); unlock it and run orch setup again",
            "macOS did not let orch use the keychain",
            "one-click could not be set up because",
            "; your key stays as it was",
            "; your workspace uses the terminal for now. Your passphrase is on your terminal: write it down.",
            "Your workspace uses the terminal for now: every approval asks for the passphrase below.",
        ),
        key=len,
        reverse=True,
    )
)


def strip_known_wording(text: str) -> str:
    """``text`` without the fixed sentences of :data:`KNOWN_WORDING` (each replaced by one space, so the neighbours
    stay separated and every check still sees whatever else is there, split or joined as before)."""
    for w in KNOWN_WORDING:
        start_alnum, end_alnum = w[0].isalnum(), w[-1].isalnum()

        def repl(m: re.Match[str], start_alnum: bool = start_alnum, end_alnum: bool = end_alnum) -> str:
            # the same notion of "letter" as the secret checks (str.isalnum): a glued edge is not a whole sentence
            if start_alnum and m.start() > 0 and m.string[m.start() - 1].isalnum():
                return m.group(0)
            if end_alnum and m.end() < len(m.string) and m.string[m.end()].isalnum():
                return m.group(0)
            return " "

        text = re.sub(re.escape(w), repl, text)
    return text


def redact_reason(text: str, secrets: Iterable[str], limit: int | None = None) -> str:
    """THE one gate for error/reason text of the setup flow. The whole text is checked BEFORE any cut (and the cut
    result again), so no cut can show part of a secret; a hit replaces the whole text with :data:`HIDDEN_REASON`.
    The checks run on the text without our own fixed sentences (:data:`KNOWN_WORDING`); everything else in it, in any
    encoding, is checked exactly as before."""
    secrets = [x for x in secrets if isinstance(x, str)]
    if contains_secret(strip_known_wording(text), secrets):
        return HIDDEN_REASON
    out = text if limit is None else text[:limit]
    return HIDDEN_REASON if contains_secret(strip_known_wording(out), secrets) else out


# ================================================================================================== the create sequence


@dataclass(frozen=True)
class Genesis:
    wid: str
    person: str
    device: str


@dataclass(frozen=True)
class Identity:
    person: str
    device: str
    auth: str  # "passphrase" | "convenience"


class Ports(Protocol):
    """Everything the create sequence does to the machine. Real: :class:`RealPorts`. Tests: a fake."""

    terminal: Any  # show(text), ask(prompt) -> str, wait(prompt)

    def genesis(self, *, root: Path, profile: dict[str, Any], passphrase: str, code: str) -> Genesis: ...
    def identity(self, wid: str) -> Identity: ...
    def install_addon(self, root: Path, name: str, raw: bytes) -> None: ...
    def git_init(self, root: Path) -> None: ...
    def runner_enable(self, wid: str, prefix: str, terminals: bool) -> None: ...
    def runner_hello(self, wid: str, timeout: float) -> bool: ...
    def records_setup(self, wid: str, root: Path) -> None: ...
    def workspace(self, wid: str) -> Any: ...  # a connection: .call(op, args, **kw) -> result dict; .close()
    def unlock(self, wid: str, passphrase: str | None, digest: str, fields: tuple[tuple[str, str], ...]) -> Any: ...
    def convenience_on(self, wid: str) -> None: ...
    def stage(self, wid: str, prefix: str, passphrase: str) -> Any: ...
    def finish_conversion(self, wid: str, staged: Any) -> None: ...
    def undo(self, wid: str, staged: Any) -> None: ...


class Run:
    """The progress of one run (``GET progress``). Thread-safe; carries no secret."""

    def __init__(self, run_id: str, steps: list[tuple[str, str]]) -> None:
        self.id = run_id
        self._lock = threading.Lock()
        self.steps = [{"id": i, "label": label, "status": "todo"} for i, label in steps]
        self.done = False
        self.error: dict[str, Any] | None = None
        self.retry: list[str] = []
        self.reasons: dict[str, str] = {}
        self.workspace: dict[str, str] | None = None
        self.waiting_tty = False
        #: called with one plain line when a step ends (the setup terminal prints it; B11 finding 25)
        self.echo: Callable[[str], None] | None = None

    def set(self, step: str, status: str) -> None:
        said = None
        with self._lock:
            for s in self.steps:
                if s["id"] == step:
                    changed = s["status"] != status
                    s["status"] = status
                    if changed and status in ("done", "failed", "skipped"):
                        said = f"  {ECHO[status]} {s['label']}"
        if said is not None and self.echo is not None:
            with contextlib.suppress(Exception):
                self.echo(said)

    def explain(self, step: str, text: str) -> None:
        """Why a step failed and what to run next (plain words); echoed to the setup terminal, shown in ``view``."""
        with self._lock:
            self.reasons[step] = text
        if self.echo is not None:
            with contextlib.suppress(Exception):
                self.echo(f"    {text}")

    def status(self, step: str) -> str | None:
        with self._lock:
            return next((s["status"] for s in self.steps if s["id"] == step), None)

    def add_retry(self, item: str) -> None:
        with self._lock:
            if item not in self.retry:
                self.retry.append(item)

    def view(self) -> dict[str, Any]:
        with self._lock:
            return {
                "steps": [dict(s) for s in self.steps],
                "done": self.done,
                "error": self.error,
                "retry": list(self.retry),
                "reasons": dict(self.reasons),
                "workspace": self.workspace,
                "waiting": "waiting for terminal" if self.waiting_tty else None,
            }


ECHO = {"done": "done:", "failed": "FAILED:", "skipped": "skipped:"}
STEP_LABELS = {
    "keys": "Making your keys",
    "genesis": "Signing the workspace",
    "register": (
        "Registering it with the host. A Mac may now show \u201cBackground Items Added\u201d for orch-host. "
        "That is expected, leave it on"
    ),
    "git": "Making the folder a git repository",
    "runner": (
        "Turning on dashboard execution. A Mac may show \u201cBackground Items Added\u201d for orch-runner. Leave it on"
    ),
    "settings": "Saving the workspace settings",
    "flags": "Turning on one-click approval",
    "runner_grant": "Giving this machine's runner agent access",
    "custody": "Moving your key to one-click custody",
    "records": "Setting up the records history on this computer",
}
HELLO_SECONDS = 30.0


def _runner_failure(exc: BaseException, safe: Callable[[str, int | None], str]) -> str:
    """The reason the runner could not be turned on, and the command to run next (one line, no control characters).
    The exception text goes through the setup redaction gate (``safe`` = ``Creator._safe``) before it is cut or shown;
    our own fixed words are added after it."""
    why = safe(str(exc), None) or type(exc).__name__
    why = " ".join("".join(c if c.isprintable() else " " for c in why).split())[:300]
    if "--replace" in why:
        return why  # it already names the command: ... To do that on purpose: orch runner enable --replace
    return f"Dashboard execution is not on: {why}. Next: orch runner enable"


class Creator:
    """Runs one reviewed plan. ``execute`` never raises: the outcome is in ``run`` (and the terminal)."""

    def __init__(
        self,
        ports: Ports,
        *,
        plan: review_mod.Plan,
        profile: dict[str, Any],
        root: Path,
        secrets: Secrets | None,
        run: Run,
        mode: str = "new",
        wid: str | None = None,
        clock: Callable[[], float] = time.time,
        cancelled: threading.Event | None = None,
        manifest_bytes: Callable[[str], bytes] | None = None,
    ) -> None:
        if manifest_bytes is None:
            from orch.addons import builtin

            manifest_bytes = builtin.manifest_bytes
        self.manifest_bytes = manifest_bytes
        self.ports, self.plan, self.profile, self.root = ports, plan, profile, root
        self.secrets, self.run, self.mode, self.wid = secrets, run, mode, wid
        if mode == "finish" and wid:  # Finish setup: the workspace already exists; the page's Done offers it
            run.workspace = {"id": wid, "prefix": profile["prefix"]}
        self.clock = clock
        self.cancelled = cancelled or threading.Event()
        self.items = list(plan.items)
        run.echo = self._say
        self._grant_skipped = False
        self._passphrase: str | None = None
        self._shown = False  # the passphrase was shown on the terminal (once)
        self._typed = False  # the person typed it in Finish: never shown back
        self._custody_done = False
        self._genesis_started = False  # ports.genesis was called: its outcome may be unknown, the key may land

    def _held_secrets(self) -> list[str]:
        out: list[str] = [self._passphrase] if self._passphrase else []
        if self.secrets is not None:
            out += self.secrets.held()
        return out

    def _safe(self, text: str, limit: int | None = None) -> str:
        """Every error/reason text the flow shows, logs or stores goes through here (see :func:`redact_reason`)."""
        return redact_reason(text, self._held_secrets(), limit)

    def _say(self, line: str) -> None:
        """One progress line on the person's own terminal (never a secret: step names only)."""
        self.ports.terminal.show(line + "\n")

    # -- the public entry
    def execute(self) -> None:
        post_genesis = self.mode == "finish"
        try:
            self._tty_confirm()
            self._stop_point()
            self._say("orch setup: working on it; the page shows the same steps")
            if self.mode == "new":
                self._genesis()
                post_genesis = True
            self._install_addons()
            self._git()
            self._runner()
            self._stop_point()
            self._batch()
            self._flags()
            self._custody()
            self._records()
            self.run.done = True
        except KeyboardInterrupt:
            self._fail(SetupError("stop", "setup was stopped from the terminal", 409), post_genesis)
        except SetupError as e:
            self._fail(e, post_genesis)
        except CustodyError as e:
            self._fail(SetupError("custody.error", f"the key custody refused ({type(e).__name__})", 409), post_genesis)
        except BaseException as e:  # noqa: BLE001 - the thread must report, never die silently
            self._fail(SetupError("internal", f"setup failed ({type(e).__name__})", 500), post_genesis)
        finally:
            if self.run.error is not None:
                self._rescue_passphrase()  # before the wipe: never leave a one-click key under an unseen passphrase
            self._passphrase = None
            if self.secrets is not None:
                self.secrets.wipe()

    def _rescue_passphrase(self) -> None:
        """HIGH-1: in one-click mode the passphrase is never shown up front. Any failure after the genesis and before
        the conversion is done (a signing mismatch, a host error, Ctrl-C, an internal error) leaves the only device key
        under it, so it is shown ONCE on the terminal before it is wiped. Residual: SIGKILL/SIGHUP of the setup process
        cannot run this (R6 notes); the recovery code is then the way back to the person key."""
        # _genesis_started, not wid: an unknown bootstrap outcome keeps the device key and it may still land
        if (
            self.mode == "new"
            and self.profile["method"] == "click"
            and self._genesis_started
            and not self._custody_done
        ):
            with contextlib.suppress(Exception):
                self._show_passphrase_once()

    def _show_passphrase_once(self, reason: str = "") -> None:
        if self._shown or self._typed or self._passphrase is None:
            return
        self._shown = True
        why = f" because {reason}" if reason else ""
        self.ports.terminal.show(
            f"\n=== orch setup: one-click could not be set up{why} ===\n"
            "Your workspace uses the terminal for now: every approval asks for the passphrase below.\n"
            "Write it down now; it is shown here once and never on the page:\n\n    "
            + self._passphrase
            + "\n\nTo try one-click again, run `orch setup` in this folder.\n"
        )

    def _stop_point(self) -> None:
        """Ctrl-C on the setup terminal sets ``cancelled``; it takes effect between steps, never in one."""
        if self.cancelled.is_set():
            raise KeyboardInterrupt

    def _fail(self, e: SetupError, post_genesis: bool) -> None:
        for s in self.run.steps:
            if s["status"] == "doing":
                self.run.set(s["id"], "failed")
        # B12 N14: the one-click switch is only effective with the key moved; a "Done" row above a failure would lie
        if not self._custody_done and self.run.status("custody") in ("failed", "skipped", "todo", "doing"):
            if self.run.status("flags") == "done":
                self.run.set("flags", "skipped")
        self.run.error = {"code": e.code, "message": self._safe(e.message, 400), "finishable": bool(post_genesis)}

    # -- R3
    def _tty_confirm(self) -> None:
        """R3, in plain words (B11): the terminal shows the words the page must show, and the person types the
        confirm word. Written straight to ``/dev/tty`` (unbuffered ``os.write``), from the run thread, right after
        the page's Create; the page says so before that click, so nobody waits for the terminal before it."""
        if not self.plan.tty_confirm:
            return
        words = " ".join(self.plan.fingerprint or [])
        self.run.waiting_tty = True
        try:
            self.ports.terminal.show(tty_confirm_text(words))
            typed = self.ports.terminal.ask(f"If they match, type {TTY_WORD}: ")
        except SetupError:
            raise
        except Exception:  # noqa: BLE001 - no terminal: nothing is created
            raise SetupError("tty.unavailable", "no terminal to confirm on; nothing was created", 409) from None
        finally:
            self.run.waiting_tty = False
        if typed.strip().lower() != TTY_WORD:
            raise SetupError("tty.declined", "the terminal did not confirm; nothing was created", 409)

    # -- keys, genesis, register (B5)
    def _genesis(self) -> None:
        assert self.secrets is not None
        for s in ("keys", "genesis", "register"):
            self.run.set(s, "doing")
        self._passphrase = self.secrets.passphrase()
        code = self.secrets.code()
        self._genesis_started = True
        try:
            g = self.ports.genesis(root=self.root, profile=self.profile, passphrase=self._passphrase, code=code)
        finally:
            del code
        for s in ("keys", "genesis", "register"):
            self.run.set(s, "done")
        self.wid = g.wid
        self.run.workspace = {"id": g.wid, "prefix": self.profile["prefix"]}

    def _git(self) -> None:
        """``git init`` of the project folder when the review said so (before the settings that declare it)."""
        if not any(f["id"] == "git" for f in self.plan.flags):
            return
        self.run.set("git", "doing")
        try:
            self.ports.git_init(self.root)
        except SetupError:
            self.run.set("git", "failed")
            raise
        except Exception as exc:  # noqa: BLE001 - one plain refusal; nothing of git's output is shown
            self.run.set("git", "failed")
            raise SetupError("git.init_failed", f"git init failed ({type(exc).__name__})", 500) from None
        self.run.set("git", "done")

    def _records(self) -> None:
        """The records step (run 8, owner 1.8): the same code as ``orch records init``, then local auto-commit on (never
        push). The person consented in the review (the ``records`` flag, part of the review digest). A failure is a
        retry item with the command to run next, never a failed setup: the workspace itself is already made."""
        if not any(f["id"] == "records" for f in self.plan.flags):
            return
        assert self.wid is not None
        self.run.set("records", "doing")
        try:
            self.ports.records_setup(self.wid, self.root)
        except Exception as exc:  # noqa: BLE001 - reported as a retry item; the rest of the setup is done
            self.run.set("records", "failed")
            self.run.add_retry("records")
            why = " ".join("".join(c if c.isprintable() else " " for c in self._safe(str(exc), None)).split())[:300]
            self.run.explain(
                "records", f"The records history is not on: {why or type(exc).__name__}. Next: orch records init"
            )
            return
        self.run.set("records", "done")

    def _install_addons(self) -> None:
        for it in self.items:
            if it.op == "addon.grant":
                name = it.args["name"]
                self.ports.install_addon(self.root, name, self._manifest(name))

    def _manifest(self, name: str) -> bytes:
        return self.manifest_bytes(name)

    # -- runner flags, hello (R4)
    def _runner(self) -> None:
        assert self.wid is not None
        wants = any(f["id"] == "runner" for f in self.plan.flags)
        grant = next((i for i in self.items if i.id == "runner_grant"), None)
        if not wants:  # nothing to switch on (already on, Finish setup): a grant still waits for the runner's hello
            if grant is not None and not self.ports.runner_hello(self.wid, HELLO_SECONDS):
                self._skip_grant()
            return
        self.run.set("runner", "doing")
        terminals = any(f["id"] == "terminals" for f in self.plan.flags)
        try:
            self.ports.runner_enable(self.wid, self.profile["prefix"], terminals)
        except Exception as exc:  # noqa: BLE001 - reported as a retry item; the rest of the setup goes on
            self.run.set("runner", "failed")
            self.run.add_retry("runner")
            self.run.explain("runner", _runner_failure(exc, self._safe))
            if grant is not None:
                self._skip_grant()
            return
        if grant is not None and not self.ports.runner_hello(self.wid, HELLO_SECONDS):
            self.run.set("runner", "done")
            self._skip_grant()
            return
        self.run.set("runner", "done")

    def _skip_grant(self) -> None:
        self.items = [i for i in self.items if i.id != "runner_grant"]
        self._grant_skipped = True
        self.run.set("runner_grant", "skipped")
        self.run.add_retry("runner_grant")

    # -- the one-unlock batch
    def _batch(self) -> None:
        if not self.items:
            return
        from orch.host import signer as host_signer
        from orch.host.control_client import new_idem

        assert self.wid is not None
        wid = self.wid
        ident = self.ports.identity(wid)
        link = self.ports.workspace(wid)
        prepared: dict[str, dict[str, Any]] = {}
        # B8: a runner grant may last at most the workspace's ``grant_hours``. When this batch sets that setting, the
        # grant can only be prepared once the setting has landed: it is prepared (and checked against the review) right
        # before ITS signature, still inside the one unlock. Every other request is checked before the first signature.
        deferred = {i.id for i in self.items if i.id == "runner_grant" and any(j.id == "settings" for j in self.items)}

        def prepare(batch: list[review_mod.Item]) -> None:
            fresh: list[dict[str, Any]] = []
            for it in batch:
                res = link.call(
                    "human.prepare",
                    {"op": it.op, "args": it.args, "signer": {"device": ident.device, "auth": ident.auth}},
                )
                p = res["data"]
                prepared[it.id] = p
                fresh.append(p)
                host_signer.check(p, workspace_id=wid, person=ident.person, device=ident.device, auth=ident.auth)
            # R1: EVERY host-prepared event equals its reviewed rows
            rows = [r for r in self.plan.rows if r["id"] in {i.id for i in batch}]
            review_mod.check_prepared(batch, rows, [p["events"] for p in fresh], self.clock())

        try:
            for it in self.items:
                self.run.set(it.id, "doing")
            try:
                prepare([i for i in self.items if i.id not in deferred])  # before the first signature
            except BaseException:
                self._cancel_requests(link, list(prepared.values()))
                for it in self.items:
                    self.run.set(it.id, "failed")
                raise
            fields = (("workspace", wid), ("requests", str(len(self.items))), ("review", self.plan.digest[:23]))
            done: list[str] = []
            with self.ports.unlock(wid, self._passphrase, self.plan.digest, fields) as backend:
                for it in self.items:
                    try:
                        if it.id in deferred:
                            prepare([it])
                        p = prepared[it.id]
                        signed = host_signer.sign(p, backend=backend, key_id="dk", workspace_id=wid)
                        link.call(
                            "human.submit", {"request_id": p["request"]["id"], "events": signed.events}, idem=new_idem()
                        )
                    except BaseException:
                        self.run.set(it.id, "failed")
                        for later in self.items[len(done) + 1 :]:
                            self.run.set(later.id, "skipped")
                        self._cancel_requests(
                            link, [prepared[i.id] for i in self.items[len(done) + 1 :] if i.id in prepared]
                        )
                        raise
                    done.append(it.id)
                    if it.id == "runner_grant":
                        self._deposit(link, p, signed.secret)
                    else:
                        self.run.set(it.id, "done")
                    signed = None  # the secret (a grant's) is not kept
        finally:
            with contextlib.suppress(Exception):
                link.close()

    def _deposit(self, link: Any, prepared: dict[str, Any], secret: str | None) -> None:
        """The runner grant, signed: hand it to the runner on this very connection (admission as ``orch grant
        --runner``). Done only on a CONFIRMED deposit (R7); anything else is a retry item. Never shown."""
        from orch.host import signer as host_signer

        if secret is None:
            self.run.set("runner_grant", "failed")
            self.run.add_retry("runner_grant")
            return
        did = prepared["request"]["id"][3:]
        out = host_signer.hand_over(lambda o, a, **kw: link.call(o, a, **kw), secret, did)
        if out.get("deposit") == "confirmed":
            self.run.set("runner_grant", "done")
        else:
            self.run.set("runner_grant", "failed")
            self.run.add_retry("runner_grant_retry")

    @staticmethod
    def _cancel_requests(link: Any, prepared: list[dict[str, Any]]) -> None:
        for p in prepared:
            with contextlib.suppress(Exception):
                link.call("human.cancel", {"request_id": p["request"]["id"]})

    # -- one-click: flag, then conversion LAST (R6)
    def _flags(self) -> None:
        if self.profile["method"] != "click" or self.run.status("flags") is None:
            return
        assert self.wid is not None
        self.run.set("flags", "doing")
        self.ports.convenience_on(self.wid)
        self.run.set("flags", "done")

    def _custody(self) -> None:
        if self.profile["method"] != "click" or self.run.status("custody") is None:
            return
        assert self.wid is not None
        wid, prefix = self.wid, self.profile["prefix"]
        self.run.set("custody", "doing")
        typed = False
        if self._passphrase is None:
            # Finish setup (e.g. after a failed conversion): this process never held the passphrase. The person types
            # it on the setup terminal only, with echo off; it is dropped when the run ends like the generated one.
            try:
                self._passphrase = self.ports.terminal.ask_secret(
                    "\nYour device passphrase (to move the key to one-click custody): "
                )
            except Exception:  # noqa: BLE001 - no terminal: nothing is moved, the key stays as it is
                self.run.set("custody", "failed")
                self.run.add_retry("custody")
                raise SetupError(
                    "tty.unavailable", "no terminal to ask the passphrase on; the key was not moved", 409
                ) from None
            typed = self._typed = True
        last: Exception | None = None
        for _attempt in (1, 2):  # retry in-process once
            staged = None
            try:
                staged = self.ports.stage(wid, prefix, self._passphrase)
                self._stop_point()  # Ctrl-C between the two steps: the staged copy is undone, the key stays as it was
                self.ports.finish_conversion(wid, staged)
            except KeyboardInterrupt:
                if staged is not None:
                    with contextlib.suppress(Exception):
                        self.ports.undo(wid, staged)
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                if staged is not None:
                    with contextlib.suppress(Exception):
                        self.ports.undo(wid, staged)
                continue
            self._custody_done = True
            self.run.set("custody", "done")
            return
        # still failing: the key stays under the passphrase, so the passphrase is shown ONCE, on this terminal only
        reason = self._reason_words(last)
        del last
        self.run.set("custody", "failed")
        self.run.add_retry("custody")
        if typed:  # the person typed it just now: it is not shown back
            raise SetupError(
                "custody.conversion_failed",
                f"one-click could not be set up because {reason}; your key stays as it was",
                500,
            )
        try:
            self._show_passphrase_once(reason)
        except Exception:  # noqa: BLE001 - no terminal: the key is unusable; say so, never print elsewhere
            raise SetupError(
                "custody.passphrase_lost", "custody conversion failed and no terminal could show the passphrase", 500
            ) from None
        raise SetupError(
            "custody.conversion_failed",
            f"one-click could not be set up because {reason}; your workspace uses the terminal for now. "
            "Your passphrase is on your terminal: write it down.",
            500,
        )

    def _reason_words(self, e: Exception | None) -> str:
        """Why the custody move failed, in one short plain line (cleaned, cut, never the passphrase)."""
        from orch import canon

        raw = str(getattr(e, "message", None) or e or "").strip()
        if not raw:
            return "this computer refused (" + (type(e).__name__ if e is not None else "unknown") + ")"
        if self._safe(raw) == HIDDEN_REASON:
            return HIDDEN_REASON
        text = " ".join(canon.clean(raw).split())
        if self._safe(text) == HIDDEN_REASON:
            return HIDDEN_REASON
        low = text.lower()
        if (
            "keychain" in low
        ):  # B12 N14: the macOS keychain wording ("interaction not allowed, -25308 ...") is not for people
            if "interaction not allowed" in low or "-25308" in low or "locked" in low:
                return "macOS did not let orch use the keychain (it is locked); unlock it and run orch setup again"
            return "macOS did not let orch use the keychain"
        if len(text) > 160:  # cut at a word, never in the middle of one
            text = text[:160].rsplit(" ", 1)[0] + "…"
        text = text.rstrip(" .;:")
        return self._safe(text[:1].lower() + text[1:])


# ======================================================================================================== the real host


class _UnlockedBackend:
    """What ``unlocked()`` yields as a signing backend: ``sign_person_event`` and ``host.signer.sign`` ask only
    for these.
    The key is unlocked once for the whole batch; the scalar is gone when the ``with`` block ends."""

    name = "passphrase"
    person_capable = True

    def __init__(self, auth: str, raw: Callable[[bytes], bytes]) -> None:
        self.auth, self._raw = auth, raw

    def sign(self, key_id: str, payload: bytes, *, action: str) -> bytes:
        return self._raw(payload)


def _as_setup_error(e: Exception) -> SetupError:
    code = getattr(e, "code", None)
    message = getattr(e, "message", None) or str(e)
    return SetupError(
        code if isinstance(code, str) else "internal", str(message), 500
    )  # no cut here: `_fail` redacts first, then cuts


class RealPorts:
    """:class:`Ports` over the real host, custody and runner. Only this class touches them."""

    def __init__(self, env: Any, state: Path, uid: int | None = None, now: Callable[[], float] = time.time) -> None:
        import os

        from orch.ops import workspace_init

        self.env, self.state = env, Path(state)
        self.uid = os.getuid() if uid is None else uid
        self.now = now
        self.terminal = workspace_init.TERMINAL

    # -- genesis (B5)
    def genesis(self, *, root: Path, profile: dict[str, Any], passphrase: str, code: str) -> Genesis:
        from types import SimpleNamespace

        from orch.custody import select
        from orch.identity import device_ref
        from orch.ops import workspace_init as wi
        from orch.ops.errors import OrchError

        ctx = SimpleNamespace(now=self.now)
        st = wi.BootstrapState()

        def factory(directory: Any) -> Any:
            return select.setup_backend(directory, passphrase)

        try:
            wi._empty_root(root)
            wi._hosted_bootstrap(
                ctx, self.state, root, profile["prefix"], profile["name"], passphrase, code, st, backend_factory=factory
            )  # type: ignore[arg-type]
        except OrchError as e:
            wi._drop_unless_pending(self.state, st.wid, st.pending)
            raise _as_setup_error(e) from None
        except BaseException:
            wi._drop_unless_pending(self.state, st.wid, st.pending)
            raise
        finally:
            wi._close(st.client)
        assert st.wid is not None
        try:
            from orch import layout as layout_mod
            from orch.instructions import write_workspace_files

            write_workspace_files(
                root, store=layout_mod.store_of(root)
            )  # as `orch setup`: a failure here leaves the workspace in place
        except Exception:  # noqa: BLE001
            pass
        pub = factory(wi.person_dir(self.state, st.wid)).public_key("dk")
        return Genesis(st.wid, st.person, device_ref(pub))

    def identity(self, wid: str) -> Identity:
        from orch.custody.select import device_backend
        from orch.host.handoff import _own_workspaces
        from orch.identity import device_ref

        entry = next(iter(_own_workspaces(self.state, self.uid, wid)), None)
        if entry is None:
            raise SetupError("setup.workspace_missing", "this workspace is not in your registry", 500)
        backend, _sel = device_backend(self.state, wid)
        person = str(entry["peers"][str(self.uid)]["person"])
        return Identity(person, device_ref(backend.public_key("dk")), str(backend.auth))

    def install_addon(self, root: Path, name: str, raw: bytes) -> None:
        from orch import layout as layout_mod
        from orch.addons import builtin

        try:
            builtin.write_manifest(str(layout_mod.store_of(root)), name, raw)
        except (OSError, ValueError, layout_mod.LayoutError) as e:
            raise SetupError("addon.install", f"cannot write addons/{name} ({type(e).__name__})", 500) from None

    # -- the runner (D-P2-31)
    def git_init(self, root: Path) -> None:
        init_repository(root)

    def records_setup(self, wid: str, root: Path) -> None:
        """``orch records init`` and ``orch records auto on`` (no ``--push``): the records tool's own code, with the
        consent already given in the setup review (the tool's terminal question is answered by that consent)."""
        import io

        from orch.records import cli as records_cli

        for argv in (["init", "--workspace", wid, "--json"], ["auto", "on", "--workspace", wid, "--json"]):
            out = io.StringIO()
            code = records_cli.main(argv, self.env, out, confirm=lambda _text: True)
            if code != 0:
                line = (out.getvalue().strip().splitlines() or ["{}"])[-1]
                try:
                    message = str(json.loads(line).get("message", "records"))
                except ValueError:
                    message = "records"
                raise SetupError("records.setup_failed", message, 500)

    def runner_enable(self, wid: str, prefix: str, terminals: bool) -> None:
        import io

        from orch.runner import cli as runner_cli

        argv = ["enable", "--workspace", wid, "--state", str(self.state)] + ([] if terminals else ["--no-terminals"])
        res = runner_cli.enable(runner_cli._parser().parse_args(argv), self.env, io.StringIO(), confirmed=True)
        if not res.get("ok"):
            raise SetupError("runner.unavailable", str((res.get("error") or {}).get("message", "runner")), 500)

    def runner_hello(self, wid: str, timeout: float) -> bool:
        from orch.runner import enablement

        link = self.workspace(wid)
        try:
            return enablement.wait_connected(
                lambda: bool(((link.call("whoami", {}).get("data") or {}).get("runner") or {}).get("connected")),
                timeout,
            )
        finally:
            with contextlib.suppress(Exception):
                link.close()

    # -- the workspace connection and the one unlock
    def workspace(self, wid: str) -> Any:
        from orch import hostclient
        from orch.host.client import Connection, Pin
        from orch.host.handoff import _own_workspaces

        entry = next(iter(_own_workspaces(self.state, self.uid, wid)), None)
        if entry is None:
            raise SetupError("setup.workspace_missing", "this workspace is not in your registry", 500)
        sock = hostclient.workspace_socket(self.env, self.state, int(entry["host_uid"]), wid)
        last: Exception | None = None
        for _ in range(40):  # the host starts serving a new workspace within a moment
            try:
                return Connection(Pin(int(entry["host_uid"]), wid, sock), client="orch setup")
            except (OSError, ConnectionError) as e:
                last = e
                time.sleep(0.25)
        raise SetupError("host.unavailable", f"the host does not serve the workspace ({type(last).__name__})", 500)

    @contextlib.contextmanager
    def unlock(
        self, wid: str, passphrase: str | None, digest: str, fields: tuple[tuple[str, str], ...]
    ) -> Iterator[Any]:
        from orch.custody import select

        if passphrase is not None:  # a new setup: the passphrase this process generated
            from orch.ops import workspace_init as wi

            backend: Any = select.setup_backend(wi.person_dir(self.state, wid), passphrase)
        else:  # Finish setup: the person's own custody; a passphrase key asks on /dev/tty only
            backend, _sel = select.device_backend(self.state, wid)
        opener = getattr(backend, "unlocked", None)
        if opener is None:  # convenience custody signs without a factor: one signature at a time
            yield backend
            return
        # B11 finding 24: the plan was read on the setup page; the terminal says so in a line and keeps the technical
        # block behind `d` (shared with `orch sign`, see custody.passphrase.note_plain_review).
        requests = dict(fields).get("requests", "")
        self.terminal.show(
            f"\norch setup will sign {requests or 'the'} item(s) you read on the setup page, with one passphrase.\n"
        )
        from orch.custody import passphrase as _pp

        _pp.note_plain_review()
        labels = batch_labels()
        with opener("dk", action="orch setup", digest=digest.split(":")[-1][:32], fields=fields, labels=labels) as raw:
            yield _UnlockedBackend(str(backend.auth), raw)

    # -- one-click (B6) and its conversion
    def convenience_on(self, wid: str) -> None:
        from orch.host import convenience

        entry = convenience.registered_entry(self.state, wid, self.uid)
        if entry is not None:  # like the CLI and the dashboard: B10 reader check and the full-mode refusal, up front
            convenience.refuse_turn_on(self.state, entry, self.uid)
        convenience.set_enabled(self.state, wid, True, self.uid)

    def stage(self, wid: str, prefix: str, passphrase: str) -> Any:
        from orch.custody import select

        return select.stage_convenience(self.state, wid, prefix=prefix, passphrase=passphrase)

    def finish_conversion(self, wid: str, staged: Any) -> None:
        from orch.custody import select

        select.finish_convenience(self.state, wid, staged)

    def undo(self, wid: str, staged: Any) -> None:
        from orch.custody import select

        select.undo_staged(self.state, wid, staged, None)


IGNORE_LINES = (f"{STORE_DIR}/", f"{WORK_DIR}/")


def init_repository(root: Path) -> None:
    """``git init`` in the project folder under orch's isolated environment (no config of the person's or the system's,
    no template, no hook), then ``.gitignore`` lines for orch's own folders. Never commits, never touches a file of the
    person's except to APPEND the missing ignore lines to a ``.gitignore`` that is already there. A folder that is
    already a repository is left alone."""
    import os
    import shutil
    import stat
    import subprocess
    import tempfile

    from orch import gitsafe

    if os.path.lexists(root / ".git"):
        return
    env = gitsafe.isolated_env(os.environ)
    from orch.cli.dashboard import _absolute_path

    git = shutil.which("git", path=_absolute_path(env.get("PATH")))
    if git is None:
        raise SetupError("git.init_failed", "git is not installed on this computer", 500)
    with tempfile.TemporaryDirectory(prefix="orch-git-template-") as empty:
        res = subprocess.run(  # noqa: S603 - a fixed argv under the isolated environment; the folder is a real path
            [git, "init", "--quiet", "--initial-branch=main", f"--template={empty}", "--", os.fspath(root)],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
        )  # fmt: skip
    if res.returncode != 0:
        raise SetupError("git.init_failed", "git init did not succeed in this folder", 500)
    path = os.fspath(root / ".gitignore")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o644)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SetupError("git.init_failed", ".gitignore is not a plain file", 500)
        have = os.read(fd, 1 << 20).decode("utf-8", "replace").splitlines()
        add = [x for x in IGNORE_LINES if x not in have and x.rstrip("/") not in have]
        if add:
            os.lseek(fd, 0, os.SEEK_END)
            size = os.fstat(fd).st_size
            lead = b"" if size == 0 or _ends_newline(fd, size) else b"\n"
            os.write(fd, lead + "".join(x + "\n" for x in add).encode())
    finally:
        os.close(fd)


def _ends_newline(fd: int, size: int) -> bool:
    import os

    return os.pread(fd, 1, size - 1) == b"\n"
