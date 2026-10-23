"""H12c-d (orch-v2-host 11A.15.10 item 10, 11A.15.16 Q1 = B): the pluggable ``PersonKeySource`` and the production
``RecoveryCodeSource``. The recovery code is typed on ``/dev/tty`` only, once per command, checked with the existing
parser, derived exactly as ``orch init`` does, matched against the expected person before any signature, held for the
``with`` block and zeroised (best effort) on every exit path. Generated codes only; the terminal is a real pty."""

from __future__ import annotations

import ast
import os
import pty
import termios
import threading
import time
from pathlib import Path

import pytest

import orch
from orch import crypto, fdwait
from orch.crypto.labels import L
from orch.custody import personkey
from orch.custody.base import PERSON_KEY_LABELS, CustodyError, NoPrompt
from orch.identity import certs
from orch.identity import recovery as rec
from orch.ops import workspace_init

FAST = 2**10
SRC = Path(orch.__file__).resolve().parent
REAL_HARDEN, REAL_OPEN_TTY = personkey._harden, personkey._open_tty


@pytest.fixture(autouse=True)
def _soft_harden(monkeypatch):
    """No ``PT_DENY_ATTACH`` on the test process (irreversible); the real one is the passphrase backend's (pinned)."""
    monkeypatch.setattr(personkey, "_harden", lambda: lambda: None)


def _fast_key(code: str):
    return rec._key_at(code, FAST)


@pytest.fixture
def fast(monkeypatch):
    """The derivation at a low scrypt N (structure tests); the production derivation has its own test."""
    monkeypatch.setattr(personkey, "_derive_scalar", lambda text: rec._scalar(text, FAST))


@pytest.fixture
def no_grant(monkeypatch):
    monkeypatch.delenv("ORCH_GRANT", raising=False)


class FakeTty:
    """A real pseudo-terminal: the source opens the slave as its ``/dev/tty``; a writer thread types ``line`` on the
    master once the prompt is on the screen (it is written after echo is turned off and typeahead flushed), and
    everything the source writes is captured.

    Bounded (Codex 10): the writer gives up waiting for the prompt after ``LIMIT`` seconds and always ends by writing
    a newline, so the reader cannot block on it; a watchdog closes both ends after ``2 * LIMIT`` (a read on the slave
    then fails with EOF or EIO on macOS and Linux); the fixture joins the writer and re-raises its errors."""

    LIMIT = 10.0

    def __init__(self, line: str | bytes | None, *, interrupt: bool = False):
        self.master, self.slave = pty.openpty()
        self.line = line.encode() if isinstance(line, str) else line
        self.interrupt = interrupt
        self.out = bytearray()
        self.opened = 0
        self.closed = 0
        self.echo_seen_off = False
        self.handler_seen = None
        self.errors: list[BaseException] = []
        self.writer: threading.Thread | None = None
        self.watchdog = threading.Timer(2 * self.LIMIT, self.shut)
        self.watchdog.daemon = True
        self.watchdog.start()

    def open(self):
        self.opened += 1
        if self.interrupt:
            raise KeyboardInterrupt
        if self.line is not None:
            self.writer = threading.Thread(target=self._type, daemon=True)
            self.writer.start()

        def close():
            self.closed += 1

        return self.slave, self.slave, close

    def _type(self):
        import signal

        try:
            deadline = time.monotonic() + self.LIMIT
            while time.monotonic() < deadline and b"code: " not in self.out:
                if fdwait.readable(self.master, 0.05):
                    self.out += os.read(self.master, 4096)
            if b"code: " not in self.out:
                raise AssertionError("the prompt never appeared")
            self.echo_seen_off = not termios.tcgetattr(self.slave)[3] & termios.ECHO
            self.handler_seen = signal.getsignal(signal.SIGTERM)
            os.write(self.master, self.line)
        except BaseException as e:  # noqa: BLE001 - re-raised by the fixture
            self.errors.append(e)
        finally:
            try:
                os.write(self.master, b"\n")
            except OSError:
                pass

    def written(self) -> str:

        while fdwait.readable(self.master, 0.05):
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.out += chunk
        return self.out.decode("utf-8", "replace").replace("\r\n", "\n")

    def shut(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def finish(self):
        self.watchdog.cancel()
        if self.writer is not None:
            self.writer.join(self.LIMIT)
            assert not self.writer.is_alive(), "the pty writer did not finish"
        self.shut()
        if self.errors:
            raise self.errors[0]


@pytest.fixture
def tty(monkeypatch):
    made: list[FakeTty] = []

    def make(line, **kw) -> FakeTty:
        t = FakeTty(line, **kw)
        made.append(t)
        monkeypatch.setattr(personkey, "_open_tty", t.open)
        return t

    yield make
    for t in made:
        t.finish()


@pytest.fixture
def spy(monkeypatch):
    """Records every buffer the module zeroises and every hardening and its undo."""
    seen = {"zeroised": [], "hardened": 0, "restored": 0}
    real = personkey._zeroise

    def zeroise(buf):
        real(buf)
        seen["zeroised"].append(buf)

    def harden():
        seen["hardened"] += 1

        def restore():
            seen["restored"] += 1

        return restore

    monkeypatch.setattr(personkey, "_zeroise", zeroise)
    monkeypatch.setattr(personkey, "_harden", harden)
    return seen


ALL = personkey.SIGN_LABELS


def _all_zero(seen) -> bool:
    return bool(seen["zeroised"]) and all(not any(b) for b in seen["zeroised"])


def _person(code: str) -> tuple[bytes, bytes]:
    pub = crypto.public_bytes(_fast_key(code))
    return pub, crypto.person_id(pub)


# --- the protocol and the label allow-list ------------------------------------------------------------------------


def test_sign_labels_are_the_r6_revocation_and_rotation_labels_never_the_delegation():
    names = personkey.SIGN_LABELS
    assert set(names) == {
        "sig_device_cert", "sig_cert_challenge", "sig_cert_rejection", "sig_revocation", "sig_device_rotate",
    }  # fmt: skip
    assert all(L[n] in PERSON_KEY_LABELS for n in names)
    assert "sig_ws_delegation" not in names


def test_the_recovery_code_source_is_a_person_key_source(fast, no_grant, tty):
    code = rec.generate_recovery_code()
    pub, pid = _person(code)
    tty(code)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=pid, action="add a device")
    with src as pk:
        assert isinstance(pk, personkey.PersonKeySource)  # inside the block: pk_pub is known (Python 3.11 hasattr)
        assert pk is src and pk.pk_pub == pub and pk.person_id == pid


def test_attribute_access_before_the_block_is_safe_for_a_runtime_protocol_check(fast, no_grant):
    """Codex 8: Python 3.11's ``isinstance`` on a runtime protocol evaluates every member with ``hasattr``; a source
    built with ``pk_pub`` answers it before the block, and nothing but ``AttributeError`` escapes ``hasattr``."""
    pub = crypto.public_bytes(crypto.generate_private_key())
    src = personkey.RecoveryCodeSource(labels=ALL, pk_pub=pub, action="x")
    assert src.pk_pub == pub and src.person_id == crypto.person_id(pub)
    assert isinstance(src, personkey.PersonKeySource)
    for name in ("person_id", "pk_pub", "sign", "signer", "derive_label_key", "__enter__", "__exit__"):
        assert hasattr(src, name)


@pytest.mark.parametrize(
    "name",
    ["sig_ws_delegation", "sig_decision", "sig_relay_auth", "sig_card_wsk", "sig_member_list", "nonsense"],
)
def test_sign_refuses_any_label_outside_the_allow_list(fast, no_grant, tty, name):
    code = rec.generate_recovery_code()
    tty(code)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x") as pk:
        payload = L.get(name, b"orch/v2/sig/nonsense|") + b"{}"
        with pytest.raises(personkey.LabelRefused) as e:
            pk.sign(name, payload)
        assert e.value.code == "custody.label_refused"


def test_sign_refuses_a_payload_that_does_not_start_with_its_label(fast, no_grant, tty):
    code = rec.generate_recovery_code()
    tty(code)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x") as pk:
        for payload in (L["sig_revocation"] + b"{}", b"{}", L["sig_ws_delegation"] + b"{}", "text"):
            with pytest.raises(personkey.LabelRefused):
                pk.sign("sig_cert_challenge", payload)
        with pytest.raises(personkey.LabelRefused):
            pk.signer("sig_cert_challenge")(L["sig_device_cert"] + b"{}")
        sig = pk.sign("sig_revocation", L["sig_revocation"] + b"{}")
        assert crypto.verify(pk.pk_pub, sig, L["sig_revocation"] + b"{}")


def test_signer_refuses_an_unknown_label_up_front(fast, no_grant, tty):
    code = rec.generate_recovery_code()
    tty(code)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x") as pk:
        with pytest.raises(personkey.LabelRefused):
            pk.signer("sig_ws_delegation")


# --- the expected person ------------------------------------------------------------------------------------------


def test_a_code_of_another_person_is_refused_before_any_signature(fast, no_grant, tty, spy):
    mine, other = rec.generate_recovery_code(), rec.generate_recovery_code()
    tty(other)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=_person(mine)[1], action="add a device")
    with pytest.raises(personkey.PersonMismatch) as e:
        src.__enter__()
    assert e.value.code == "custody.person_mismatch"
    assert _all_zero(spy) and spy["restored"] == spy["hardened"] == 1
    with pytest.raises(personkey.PersonKeyClosed):
        src.sign("sig_revocation", L["sig_revocation"] + b"{}")


def test_the_expected_person_may_be_given_as_pk_pub(fast, no_grant, tty):
    mine, other = rec.generate_recovery_code(), rec.generate_recovery_code()
    tty(mine)
    with personkey.RecoveryCodeSource(labels=ALL, pk_pub=_person(mine)[0], action="x") as pk:
        assert pk.pk_pub == _person(mine)[0]
    tty(mine)
    with pytest.raises(personkey.PersonMismatch):
        with personkey.RecoveryCodeSource(labels=ALL, pk_pub=_person(other)[0], action="x"):
            pass


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"person_id": bytes(16), "pk_pub": crypto.public_bytes(crypto.generate_private_key())},
        {"person_id": bytes(15)},
        {"person_id": "p_" + "00" * 16},
        {"pk_pub": b"\x04" + bytes(64)},
    ],
)
def test_the_expected_person_is_exactly_one_valid_value(kw):
    with pytest.raises(CustodyError):
        personkey.RecoveryCodeSource(labels=ALL, action="x", **kw)


@pytest.mark.parametrize(
    "typed,reason",
    [
        ("abandon " * 23 + "abandon", "recovery.checksum"),
        ("abandon " * 22 + "abandon", "recovery.words"),
        ("abandon " * 23 + "notaword", "recovery.unknown_word"),
        (b"\xff\xfe", "recovery.words"),
        ("x" * 900, "recovery.words"),
    ],
)
def test_a_bad_code_is_refused_with_the_parser_reason(fast, no_grant, tty, spy, typed, reason):
    tty(typed)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x")
    with pytest.raises(personkey.BadRecoveryCode) as e:
        src.__enter__()
    assert e.value.code == "custody.recovery_code" and e.value.reason == reason
    assert e.value.__cause__ is None and (e.value.__context__ is None or e.value.__suppress_context__)
    assert _all_zero(spy) and spy["restored"] == 1


# --- least privilege: the labels a command names (Opus F4) ------------------------------------------------------------


ADD_DEVICE = ("sig_cert_challenge", "sig_cert_rejection", "sig_device_cert")
REVOKE = ("sig_revocation",)


@pytest.mark.parametrize("labels", [(), ("sig_ws_delegation",), ("sig_revocation", "nonsense"), "sig_revocation", None])
def test_labels_are_a_required_non_empty_subset(labels):
    with pytest.raises(personkey.LabelRefused):
        personkey.RecoveryCodeSource(labels=labels, person_id=bytes(16), action="x")
    with pytest.raises(TypeError):
        personkey.RecoveryCodeSource(person_id=bytes(16), action="x")  # type: ignore[call-arg]


@pytest.mark.parametrize("granted,refused", [(ADD_DEVICE, REVOKE), (REVOKE, ADD_DEVICE)])
def test_a_source_signs_only_the_labels_its_command_named(fast, no_grant, tty, granted, refused):
    code = rec.generate_recovery_code()
    tty(code)
    with personkey.RecoveryCodeSource(labels=granted, person_id=_person(code)[1], action="x") as pk:
        for name in granted:
            pk.sign(name, L[name] + b"{}")
        for name in refused:
            with pytest.raises(personkey.LabelRefused):
                pk.sign(name, L[name] + b"{}")
            with pytest.raises(personkey.LabelRefused):
                pk.signer(name)


# --- no typed code in the exception chain or its frames (Opus F1) -----------------------------------------------------


def _secret_texts(code: str) -> list[str]:
    words = code.split()
    return [code, " ".join(words[:3]), " ".join(words[-3:])]


def _leaks(exc: BaseException, code: str, scalar: int | None) -> list[str]:
    """Every place in ``exc``'s chain (``__cause__``, ``__context__``) and in every frame its tracebacks hold (but the
    test function's own, which holds the generated code by construction), where the typed code (or three consecutive
    words of it) or the scalar is still reachable."""
    texts = _secret_texts(code)
    raw = [t.encode() for t in texts]
    found: list[str] = []
    seen: set[int] = set()

    def check(where: str, value: object) -> None:
        if isinstance(value, str) and any(t in value for t in texts):
            found.append(where)
        elif isinstance(value, (bytes, bytearray)) and any(r in bytes(value) for r in raw):
            found.append(where)
        elif type(value) is int and scalar is not None and value == scalar:
            found.append(where)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                check(f"{where}[{i}]", v)

    stack: list[BaseException | None] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        check(f"{type(e).__name__}.args", e.args)
        check(f"{type(e).__name__}.str", str(e))
        for attr in ("object", "reason"):
            if hasattr(e, attr):
                check(f"{type(e).__name__}.{attr}", getattr(e, attr))
        tb = e.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            if not frame.f_code.co_name.startswith("test_"):
                for name, value in frame.f_locals.items():
                    check(f"{frame.f_code.co_name}:{name}", value)
            tb = tb.tb_next
        stack += [e.__cause__, e.__context__]
    return found


def _secret_code_with_bad_checksum() -> str:
    words = rec.generate_recovery_code().split()
    for w in ("abandon", "ability"):
        candidate = " ".join([*words[:-1], w])
        try:
            rec.check_recovery_code(candidate)
        except rec.RecoveryCodeError:
            return candidate
    raise AssertionError("unreachable")


def test_a_bad_checksum_leaves_no_typed_text_in_the_exception(fast, no_grant, tty):
    typed = _secret_code_with_bad_checksum()
    tty(typed)
    with pytest.raises(personkey.BadRecoveryCode) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert _leaks(e.value, typed, None) == []


def test_an_undecodable_line_leaves_no_typed_bytes_in_the_exception(fast, no_grant, tty):
    words = rec.generate_recovery_code()
    tty(words.encode() + b" \xff")
    with pytest.raises(personkey.BadRecoveryCode) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert _leaks(e.value, words, None) == []


@pytest.mark.parametrize("failure", [MemoryError, ValueError, KeyboardInterrupt])
def test_a_failed_derivation_of_a_valid_code_leaves_no_code_in_the_exception(no_grant, tty, monkeypatch, failure):
    code = rec.generate_recovery_code()

    def derive(text):
        local_copy = text  # noqa: F841 - a frame that holds the valid code
        raise failure("derivation failed")

    monkeypatch.setattr(personkey, "_derive_scalar", derive)
    tty(code)
    want = KeyboardInterrupt if failure is KeyboardInterrupt else personkey.PersonKeyError
    with pytest.raises(want) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert _leaks(e.value, code, None) == []


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), SystemExit(4), RuntimeError("parser bug")])
def test_an_interrupt_in_the_parser_leaves_no_typed_text(fast, no_grant, tty, monkeypatch, failure):
    """Opus N1: a ``BaseException`` raised by the code check itself (before scrypt) is replaced like a failed
    derivation: cleared frames, a fresh exception of the same type (an ``Exception`` becomes ``PersonKeyError``)."""
    code = rec.generate_recovery_code()

    def check(text):
        local_copy = text  # noqa: F841 - a parser frame that holds the typed text
        raise failure

    monkeypatch.setattr(rec, "check_recovery_code", check)
    tty(code)
    want = personkey.PersonKeyError if isinstance(failure, Exception) else type(failure)
    with pytest.raises(want) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value is not failure
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert _leaks(e.value, code, None) == []
    if isinstance(failure, SystemExit):
        assert e.value.code == 4


@pytest.mark.parametrize("exit_code,kept", [(3, 3), (None, None), ("text of the code", None)])
def test_a_fresh_system_exit_keeps_an_int_exit_code_only(no_grant, tty, monkeypatch, exit_code, kept):
    """Opus N2: the replaced ``SystemExit`` keeps an ``int``/``None`` exit code; a ``str`` one is dropped."""

    def derive(text):
        raise SystemExit(exit_code)

    monkeypatch.setattr(personkey, "_derive_scalar", derive)
    tty(rec.generate_recovery_code())
    with pytest.raises(SystemExit) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.code == kept and e.value.__context__ is None


def test_a_mismatch_leaves_no_code_or_scalar_in_the_exception(fast, no_grant, tty):
    code = rec.generate_recovery_code()
    tty(code)
    with pytest.raises(personkey.PersonMismatch) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.__cause__ is None and e.value.__context__ is None
    assert _leaks(e.value, code, rec._scalar(code, FAST)) == []


def test_an_interrupt_while_reading_leaves_no_typed_bytes_in_the_exception(fast, no_grant, tty, monkeypatch):
    code = rec.generate_recovery_code()
    t = tty(code)
    real = os.read
    count = [0]
    enough = len(" ".join(code.split()[:4]))

    def read(fd, n):
        out = real(fd, n)
        if fd == t.slave:
            count[0] += len(out)
            if count[0] > enough:
                raise KeyboardInterrupt
        return out

    monkeypatch.setattr(personkey.os, "read", read)
    with pytest.raises(KeyboardInterrupt) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert _leaks(e.value, code, None) == []


# --- the terminal only, a person only, one prompt -----------------------------------------------------------------


def test_no_terminal_is_no_prompt(fast, no_grant, monkeypatch, spy):
    def none():
        raise NoPrompt("no controlling terminal")

    monkeypatch.setattr(personkey, "_open_tty", none)
    with pytest.raises(NoPrompt) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.code == "custody.no_prompt"
    assert spy["hardened"] == 0  # Opus F3: no irreversible PT_DENY_ATTACH for a refusal


def test_the_terminal_is_opened_before_hardening_and_the_prompt_after(fast, no_grant, tty, monkeypatch):
    code = rec.generate_recovery_code()
    t = tty(code)
    order: list[str] = []
    real_open, real_read = t.open, personkey._read_code

    def opened():
        order.append("open")
        return real_open()

    def harden():
        order.append("harden")
        return lambda: order.append("restore")

    def read(*a):
        order.append("prompt")
        return real_read(*a)

    monkeypatch.setattr(personkey, "_open_tty", opened)
    monkeypatch.setattr(personkey, "_harden", harden)
    monkeypatch.setattr(personkey, "_read_code", read)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x"):
        order.append("block")
    assert order == ["open", "harden", "prompt", "block", "restore"]


def test_the_production_terminal_is_dev_tty_never_stdin():
    """The seam defaults to the passphrase backend's ``/dev/tty`` opener (never stdin, stdout or stderr)."""
    from orch.custody import passphrase

    assert REAL_OPEN_TTY is passphrase._open_tty and REAL_HARDEN is passphrase._harden
    tree = ast.parse((SRC / "custody" / "personkey.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & {"stdin", "stdout", "stderr", "input", "getpass", "print"}


@pytest.mark.parametrize("value", ["g1.secret", "0", " "])
def test_orch_grant_refuses_before_any_prompt(fast, monkeypatch, tty, value):
    monkeypatch.setenv("ORCH_GRANT", value)
    t = tty(rec.generate_recovery_code())
    with pytest.raises(personkey.HumanOnly) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.code == "human_only" and t.opened == 0


def test_one_prompt_serves_the_whole_block_and_a_source_is_used_once(fast, no_grant, tty):
    code = rec.generate_recovery_code()
    t = tty(code)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="add the device phone")
    with src as pk:
        for _ in range(3):
            pk.sign("sig_revocation", L["sig_revocation"] + b"{}")
        pk.derive_label_key()
    assert t.opened == 1 and t.closed == 1
    with pytest.raises(personkey.PersonKeyClosed):
        src.__enter__()
    assert t.opened == 1


def test_the_prompt_names_the_action_and_person_and_echo_is_off(fast, no_grant, tty):
    """Codex 9: the terminal shows exactly the prompt and the newline after the line; nothing typed is echoed."""
    code = rec.generate_recovery_code()
    pid = _person(code)[1]
    t = tty(code)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=pid, action="add the device phone"):
        pass
    assert t.echo_seen_off
    assert t.written() == personkey._prompt("add the device phone", pid) + "\n"
    shown = personkey._prompt("add the device phone", pid)
    assert "For: add the device phone\n" in shown and "p_" + pid.hex() in shown and "recovery code" in shown


@pytest.mark.parametrize(
    "action,shown",
    [
        ("add \x1b[2J the\x1b]0;evil\x07 phone", "add [2J the]0;evil phone"),
        ("revoke \u202ephone\u202c now", "revoke phone now"),
        ("a\x85b\x9bc\u2028d\u200be\tf\ng", "a bc de f g"),
        ("x" * 500, "x" * personkey.MAX_ACTION_CHARS),
        (b"bytes", ""),
    ],
)
def test_the_action_reaches_the_terminal_printable_only_and_capped(action, shown):
    """Opus F2: no ESC, CSI, OSC, C1, bidi or other format controls reach the terminal; at most MAX_ACTION_CHARS."""
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action=action)
    prompt = personkey._prompt(src._action, bytes(16))
    assert src._action == shown
    assert all(c.isprintable() or c == "\n" for c in prompt)


def test_echo_is_restored_on_sigterm_and_sighup_during_the_prompt(fast, no_grant, tty, monkeypatch):
    """Opus F5: while the prompt waits, SIGTERM and SIGHUP (when their handler is the default) restore the terminal's
    echo and the previous handlers, then re-raise with the default action; afterwards the handlers are the old ones."""
    import signal

    code = rec.generate_recovery_code()
    t = tty(code)
    before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x"):
        pass
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before
    if before[0] is not signal.SIG_DFL:
        pytest.skip("the test runner has its own SIGTERM handler")
    handler = t.handler_seen
    assert callable(handler) and handler is not signal.SIG_DFL
    raised: list[int] = []
    monkeypatch.setattr(personkey.signal, "raise_signal", raised.append)
    attrs = termios.tcgetattr(t.slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(t.slave, termios.TCSANOW, attrs)
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGHUP, handler)
    try:
        handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, before[0])
        signal.signal(signal.SIGHUP, before[1])
    assert raised == [signal.SIGTERM]
    assert termios.tcgetattr(t.slave)[3] & termios.ECHO


# --- the lifecycle: zeroised on every exit path -------------------------------------------------------------------


def test_signing_before_and_after_the_block_refuses(fast, no_grant, tty, spy):
    code = rec.generate_recovery_code()
    tty(code)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x")
    for call in (lambda: src.sign("sig_revocation", L["sig_revocation"]), src.derive_label_key):
        with pytest.raises(personkey.PersonKeyClosed):
            call()
    with src as pk:
        signer = pk.signer("sig_revocation")
        signer(L["sig_revocation"] + b"{}")
    assert _all_zero(spy) and spy["restored"] == spy["hardened"] == 1
    for call in (
        lambda: src.sign("sig_revocation", L["sig_revocation"]),
        src.derive_label_key,
        lambda: signer(L["sig_revocation"] + b"{}"),
    ):
        with pytest.raises(personkey.PersonKeyClosed) as e:
            call()
        assert e.value.code == "custody.person_key_closed"


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt(), SystemExit(3)])
def test_an_exception_in_the_block_zeroises_and_propagates(fast, no_grant, tty, spy, exc):
    code = rec.generate_recovery_code()
    tty(code)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x")
    with pytest.raises(type(exc)):
        with src as pk:
            signer = pk.signer("sig_revocation")
            raise exc
    assert _all_zero(spy) and spy["restored"] == 1
    with pytest.raises(personkey.PersonKeyClosed):
        signer(L["sig_revocation"] + b"{}")


def test_a_source_entered_by_hand_and_dropped_is_zeroised_when_collected(fast, no_grant, tty, spy):
    import gc

    code = rec.generate_recovery_code()
    tty(code)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x")
    src.__enter__()
    del src
    gc.collect()
    assert _all_zero(spy) and spy["restored"] == 1


def test_a_keyboard_interrupt_while_opening_the_terminal_hardens_nothing(fast, no_grant, tty, spy):
    t = tty(None, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert spy["restored"] == spy["hardened"] == 0 and t.closed == 0  # the opener raised before it opened anything


def test_a_keyboard_interrupt_while_reading_zeroises_the_partial_code(fast, no_grant, tty, spy, monkeypatch):
    t = tty("abandon abandon")
    real = os.read
    calls = {"n": 0}

    def read(fd, n):
        if fd == t.slave:
            calls["n"] += 1
            if calls["n"] > 5:
                raise KeyboardInterrupt
        return real(fd, n)

    monkeypatch.setattr(personkey.os, "read", read)
    with pytest.raises(KeyboardInterrupt):
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert t.closed == 1 and _all_zero(spy) and spy["restored"] == 1
    assert termios.tcgetattr(t.slave)[3] & termios.ECHO  # the terminal's echo is back


def test_a_failure_while_deriving_zeroises(fast, no_grant, tty, spy, monkeypatch):
    def boom(text):
        raise MemoryError

    monkeypatch.setattr(personkey, "_derive_scalar", boom)
    tty(rec.generate_recovery_code())
    with pytest.raises(personkey.PersonKeyError) as e:
        personkey.RecoveryCodeSource(labels=ALL, person_id=bytes(16), action="x").__enter__()
    assert e.value.code == "custody.person_key" and e.value.__cause__ is None and e.value.__context__ is None
    assert _all_zero(spy) and spy["restored"] == 1


# --- no secret in a repr, an exception text or a copy --------------------------------------------------------------


def test_no_secret_in_repr_or_exception_text(fast, no_grant, tty):
    # fixed codes, not random ones: the check below is "no word of the secret appears in the text", and the error text
    # holds ordinary words that are also in the code word list (one, not, code, nothing, ...), so two random codes
    # collided with it in roughly one run in twenty. These two do not, and the assertions stay exactly as strict.
    mine, other = rec.code_from_entropy(bytes(range(32))), rec.code_from_entropy(bytes(range(32, 64)))
    secret_words = set(other.split())
    scalar = rec._scalar(other, FAST).to_bytes(32, "big")
    tty(other)
    src = personkey.RecoveryCodeSource(labels=ALL, person_id=_person(mine)[1], action="x")
    with pytest.raises(personkey.PersonMismatch) as e:
        src.__enter__()
    texts = [repr(src), str(src), str(e.value), repr(e.value), *map(str, e.value.args)]
    for text in texts:
        assert not secret_words & set(text.replace(",", " ").replace("'", " ").split())
        assert scalar.hex() not in text and str(int.from_bytes(scalar, "big")) not in text
    tty(mine)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=_person(mine)[1], action="x") as pk:
        for text in (repr(pk), str(pk)):
            assert not set(mine.split()) & set(text.split())
            assert rec._scalar(mine, FAST).to_bytes(32, "big").hex() not in text


def test_a_source_cannot_be_copied_or_pickled(fast, no_grant, tty):
    import copy
    import pickle

    code = rec.generate_recovery_code()
    tty(code)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=_person(code)[1], action="x") as pk:
        for f in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                f(pk)
        assert not hasattr(pk, "__dict__")


# --- the derivation is init's ----------------------------------------------------------------------------------------


def test_derive_label_key_and_the_key_equal_init_for_the_same_code(no_grant, tty):
    """Production scrypt parameters: the same person key and label key as ``orch init``'s derivation."""
    code = rec.generate_recovery_code()
    pk = rec.derive_person_key(code)
    want_pub = crypto.public_bytes(pk)
    tty("  " + code.upper().replace(" ", "   ") + "  ")  # normalised by the existing parser
    with personkey.RecoveryCodeSource(labels=ALL, pk_pub=want_pub, action="x") as src:
        assert src.derive_label_key() == workspace_init._label_key(crypto.private_scalar(pk))
        assert src.person_id == crypto.person_id(want_pub)


def test_the_label_key_opens_a_label_init_sealed(fast, no_grant, tty):
    code = rec.generate_recovery_code()
    key = _fast_key(code)
    pid = crypto.person_id(crypto.public_bytes(key))
    dev = crypto.device_id(crypto.public_bytes(crypto.generate_private_key()))
    sealed = certs.seal_device_label(workspace_init._label_key(crypto.private_scalar(key)), pid, dev, "first device")
    tty(code)
    with personkey.RecoveryCodeSource(labels=ALL, person_id=pid, action="x") as src:
        assert certs.open_device_label(src.derive_label_key(), pid, dev, sealed) == "first device"


# --- end to end: a cert challenge the host accepts ------------------------------------------------------------------


def _hex() -> str:
    return os.urandom(16).hex()


# --- the boundary: who may construct the source and touch the raw scalar ---------------------------------------------


def _refs(names: set[str]) -> dict[str, set[str]]:
    """``{module path relative to orch: names used}`` for every name in ``names`` (Name, Attribute, import alias)."""
    out: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hit = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in names:
                hit.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr in names:
                hit.add(node.attr)
            elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] in names:
                hit.add(node.name.rsplit(".", 1)[-1])
            elif isinstance(node, ast.Constant) and node.value in names:
                hit.add(node.value)
        if hit:
            out[path.relative_to(SRC).as_posix()] = hit
    return out


# Each entry is a reviewed caller. R6 (orch-29) adds its add-device and revoke call sites here, with the review.
SOURCE_CALLERS = {"custody/personkey.py", "custody/cli.py"}  # B1b: orch keys primary init (account creation)
SCALAR_CALLERS = {
    "identity/recovery.py",  # the derivation itself
    "identity/__init__.py",  # the re-export
    "ops/workspace_init.py",  # orch init: the genesis certificate and delegation, then dropped
    "custody/personkey.py",  # the RecoveryCodeSource (Q1 = B)
}


def test_only_reviewed_modules_construct_the_recovery_code_source():
    assert set(_refs({"RecoveryCodeSource"})) <= SOURCE_CALLERS


def test_only_reviewed_modules_derive_the_person_scalar():
    used = _refs({"derive_person_scalar", "derive_person_key", "_scalar", "_key_at"})
    used.pop("custody/describe.py", None)  # an unrelated local helper named ``_scalar`` (display of JSON scalars)
    assert set(used) <= SCALAR_CALLERS


def test_only_reviewed_modules_name_the_label_key_derivation():
    assert set(_refs({"orch/v2/pvk|p1"})) <= {"ops/workspace_init.py", "custody/personkey.py"}
    assert set(_refs({b"orch/v2/pvk|p1"})) <= {"ops/workspace_init.py", "custody/personkey.py"}


def test_the_raw_scalar_never_leaves_the_source():
    """Inside ``personkey.py`` the scalar buffer and the key object are reached only through the private slot
    ``_live`` of ``self``; no public method returns them."""
    tree = ast.parse((SRC / "custody" / "personkey.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "_live":
            assert isinstance(node.value, ast.Name) and node.value.id == "self"
    public = [n for n in dir(personkey.RecoveryCodeSource) if not n.startswith("_")]
    assert set(public) == {"person_id", "pk_pub", "sign", "signer", "derive_label_key"}


def test_the_sandbox_source_is_never_in_the_package():
    assert not list(SRC.rglob("*personkey_sandbox*"))
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            mods += [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any("personkey_sandbox" in m or m.split(".")[0] == "tests" for m in mods), path
