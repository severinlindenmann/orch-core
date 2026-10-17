"""Setup error text never shows a secret: one gate (redact_reason) for the page, the terminal and run.error."""

from __future__ import annotations

from orch.setup.protocol import SetupError
from orch.setup.steps import HIDDEN_REASON, Creator, Run, Secrets, redact_reason


def creator(passphrase: str | None = None, with_secrets: bool = True) -> tuple[Creator, Secrets | None]:
    c = Creator.__new__(Creator)
    c._passphrase = passphrase
    c.secrets = Secrets(show_passphrase=True) if with_secrets else None
    c.run = Run("run_t", [("custody", "Custody")])
    c.mode, c.profile = "new", {"method": "click"}
    c._custody_done = False
    return c, c.secrets


def test_passphrase_split_over_lines_is_redacted():
    c, s = creator()
    s.confirmed = True
    pw = s.passphrase()
    split = pw.replace(" ", "\n").replace("-", "\t")
    c._passphrase = pw
    assert c._reason_words(RuntimeError(f"bad thing\n{split}\nend")) == HIDDEN_REASON
    c._fail(SetupError("x", f"oops {split.upper()}", 500), True)
    assert c.run.error["message"] == HIDDEN_REASON


def test_recovery_code_is_redacted_whole_and_as_words():
    c, s = creator()
    s.confirmed = True
    code = s.code()
    assert c._reason_words(RuntimeError("failed with " + code)) == HIDDEN_REASON
    assert c._reason_words(RuntimeError("failed with " + "\n".join(code.split()))) == HIDDEN_REASON
    assert c._reason_words(RuntimeError("failed with " + ", ".join(code.split()))) == HIDDEN_REASON
    c._fail(SetupError("x", code, 500), True)
    assert c.run.error["message"] == HIDDEN_REASON


def test_secret_hidden_before_confirm_too():
    c, s = creator()
    code = s._code.decode()
    assert redact_reason("x " + code, s.held()) == HIDDEN_REASON


def test_reason_without_secret_passes_unchanged():
    c, _ = creator()
    assert c._reason_words(RuntimeError("the host said no")) == "the host said no"
    c._fail(SetupError("x", "plain message", 500), False)
    assert c.run.error["message"] == "plain message"


def test_cut_at_400_never_exposes_part_of_a_secret():
    c, s = creator()
    s.confirmed = True
    code = s.code()
    for pad in (350, 380, 395, 399, 400, 401, 430):
        msg = "a" * pad + " " + code
        out = redact_reason(msg, s.held(), 400)
        assert out == HIDDEN_REASON
        c._fail(SetupError("x", msg, 500), False)
        assert c.run.error["message"] == HIDDEN_REASON
        for w in code.split():
            assert w not in c.run.error["message"]


def test_wiped_secrets_and_odd_input_do_not_raise():
    c, s = creator()
    s.wipe()
    assert s.held() == []
    assert redact_reason("hello", [None, "", "  "]) == "hello"  # type: ignore[list-item]


def _held():
    s = Secrets(show_passphrase=True)
    s.confirmed = True
    return s, s.passphrase(), s.code()


def test_secret_crossing_offset_300_in_a_port_error_is_hidden():
    from orch.setup.steps import _as_setup_error

    c, s = creator()
    s.confirmed = True
    pw = s.passphrase()
    c._passphrase = pw
    for pad in (280, 290, 297, 300):
        e = RuntimeError("x" * pad + " " + pw)
        c._fail(_as_setup_error(e), True)
        assert c.run.error["message"] == HIDDEN_REASON


def test_half_a_passphrase_and_single_words_are_hidden():
    s, pw, code = _held()
    long_words = [w for w in pw.split("-") + pw.split() if len(w) >= 6]
    assert long_words, pw
    assert redact_reason(f"it said {long_words[0]} twice", s.held()) == HIDDEN_REASON
    rw = [w for w in code.split() if len(w) >= 6]
    if rw:
        assert redact_reason(f"it said {rw[0]}", s.held()) == HIDDEN_REASON


def test_word_pair_joined_is_hidden():
    s, pw, code = _held()
    w = code.split()
    for a, b in zip(w, w[1:]):
        if len(a + b) >= 6:
            assert redact_reason(f"x {a}{b} y", s.held()) == HIDDEN_REASON
            assert redact_reason(f"x {a}\n{b} y", s.held()) == HIDDEN_REASON
            break
    p = pw.replace("-", " ").split()
    assert redact_reason(f"x {p[0]}.{p[1]} y", s.held()) == HIDDEN_REASON


def test_encoded_forms_are_hidden():
    import base64
    import urllib.parse

    s, pw, code = _held()
    for sec in (pw, code):
        b = sec.encode()
        forms = [
            b.hex(),
            b.hex().upper(),
            base64.b64encode(b).decode(),
            base64.urlsafe_b64encode(b).decode().rstrip("="),
            urllib.parse.quote(sec),
            urllib.parse.quote_plus(sec),
            sec.replace(" ", "%20"),
        ]
        for f in forms:
            assert redact_reason(f"err: {f} end", s.held()) == HIDDEN_REASON, f
        # base64 inside a longer encoded blob (other alignments)
        for k in range(3):
            blob = base64.b64encode(b"q" * k + b + b"zz").decode()
            assert redact_reason(f"blob {blob}", s.held()) == HIDDEN_REASON, k


def test_ordinary_text_still_passes():
    s, _, _ = _held()
    assert redact_reason("the host said no (code 42)", s.held()) == "the host said no (code 42)"
