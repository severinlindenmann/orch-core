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
    # fixed tokens that are in no word list: a random recovery word must never be a substring of these
    assert c._reason_words(RuntimeError("zqx7 qqz9 xk-7")) == "zqx7 qqz9 xk-7"
    c._fail(SetupError("x", "zqx7 xk-7 qqz9", 500), False)
    assert c.run.error["message"] == "zqx7 xk-7 qqz9"


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
        # the message is exactly the fixed hidden text: a per-word check would flake when a random recovery word is a
        # substring of that fixed text


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
    for a, b in zip(w, w[1:], strict=False):
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


# ---- word collisions: a secret word inside OUR OWN fixed sentence is not a leak; anywhere else it still is ----

_FIXED = "macOS did not let orch use the keychain"


def test_a_secret_word_inside_a_fixed_sentence_does_not_hide_the_reason():
    for secrets in (
        ["use the let orch did not macos unlock zoo", "use", "the", "let", "orch", "did", "macos"],
        ["abandon use the keychain", "keychain", "use", "the", "usethe", "letorch", "notlet"],
        ["orchuse thekeychain keychain"],
    ):
        assert redact_reason(_FIXED, secrets) == _FIXED, secrets
    full = "one-click could not be set up because " + _FIXED + "; your key stays as it was"
    held = ["use the keychain", "keychain", "stays as it was now", "uses", "terminal"]
    assert redact_reason(full, held, 400) == full


def test_a_secret_word_anywhere_else_still_hides_the_text():
    held = ["keychain", "spoon tray", "use the let orch", "usethe"]
    for text in (
        "macOS did not let orch use the keychain; the word was keychain",  # the word again outside the sentence
        "macOS did not let orch use the keychain spoon tray",  # a pasted pair outside
        _FIXED + " and then use the let orch",  # the whole secret outside
        _FIXED + " (usethe)",
    ):
        assert redact_reason(text, held) == HIDDEN_REASON, text
    # secret words only in the fixed part are fine; the same secret split by line breaks elsewhere is not
    assert redact_reason(_FIXED + "\nuse\nthe\nlet\norch", ["use the let orch"]) == HIDDEN_REASON
    assert redact_reason(_FIXED + " ok", ["use the let orch"]) == _FIXED + " ok"


def test_encoded_secrets_next_to_a_fixed_sentence_are_hidden():
    import base64
    import urllib.parse

    w = "keychain"
    code = "use the let orch spoon tray"
    for sec in (w, code):
        b = sec.encode()
        for f in (
            b.hex(),
            base64.b64encode(b).decode(),
            urllib.parse.quote(sec),
            urllib.parse.quote_plus(sec),
            sec.replace(" ", "%20"),
        ):
            assert redact_reason(f"{_FIXED}: {f}", [sec, *sec.split()]) == HIDDEN_REASON, f
    # a base64 secret split by spaces / line breaks (whitespace-removed comparison)
    for sec in (w, code):
        enc = base64.b64encode(b"q" + sec.encode() + b"zz").decode()
        split = " ".join(enc[i : i + 4] for i in range(0, len(enc), 4))
        assert redact_reason(f"{_FIXED}\n{split}", [sec, *sec.split()]) == HIDDEN_REASON, sec
        assert redact_reason(f"{_FIXED}\n" + "\n".join(split.split()), [sec, *sec.split()]) == HIDDEN_REASON, sec


def test_stripping_is_a_closed_exact_list():
    from orch.setup.steps import KNOWN_WORDING, strip_known_wording

    assert strip_known_wording("a " + _FIXED + " b").split() == ["a", "b"]
    glued = "macOS did not let orch use the keychains"
    assert strip_known_wording(glued) == glued
    assert strip_known_wording(_FIXED.lower()) == _FIXED.lower()
    assert all(len(w) > 20 for w in KNOWN_WORDING)  # whole sentences, never a bare word


def test_a_secret_running_into_a_fixed_sentence_edge_is_hidden():
    assert redact_reason("rando" + _FIXED, ["random"]) == HIDDEN_REASON
    assert redact_reason(_FIXED + "x", ["keychainx"]) == HIDDEN_REASON
    assert redact_reason("abc" + _FIXED, ["abcmacos"]) == HIDDEN_REASON


def test_non_ascii_letters_glued_to_a_fixed_sentence_are_hidden():
    assert redact_reason("caf\u00e9" + _FIXED, ["caf\u00e9macos"]) == HIDDEN_REASON
    assert redact_reason(_FIXED + "\u00e9", ["keychain\u00e9"]) == HIDDEN_REASON
    assert redact_reason("\ufb01" + _FIXED, ["\ufb01macos"]) == HIDDEN_REASON


# -- a filesystem path in a message is not a leak (flaky test_item5 root cause: a random recovery word such as
# "person" occurs in "/h/.config/orch/person"; deterministic here by choosing that word)

PATH_MSG = "/h/.config/orch/person is writable by others (mode 770); run: chmod 700 /h/.config/orch/person"


def test_a_secret_word_inside_a_path_does_not_hide_the_message():
    from orch.setup.steps import Word, _Code

    secrets = [
        _Code("person alpha bravo charlie"),
        *map(Word, "person alpha bravo charlie".split()),
        "orchwritable-pass",
    ]
    assert redact_reason(PATH_MSG, secrets) == PATH_MSG
    assert redact_reason("cannot read ~/.orch/person/key: denied", secrets) != HIDDEN_REASON


def test_a_secret_word_outside_a_path_is_still_hidden():
    from orch.setup.steps import Word, _Code

    secrets = [_Code("person alpha bravo charlie"), Word("person")]
    assert redact_reason("the person is not allowed", secrets) == HIDDEN_REASON
    assert redact_reason(PATH_MSG + " and person", secrets) == HIDDEN_REASON


def test_a_whole_secret_is_hidden_even_inside_a_path():
    s = Secrets(show_passphrase=True)
    code = bytes(s._code).decode("ascii")
    pw = bytes(s._pass).decode("ascii")
    for secret in (code, pw):
        assert redact_reason(f"cannot open /home/{secret.replace(' ', '/')}/x", s.held()) == HIDDEN_REASON
        assert redact_reason(f"cannot open /home/{secret.replace(' ', '')}/x", s.held()) == HIDDEN_REASON


def test_one_held_recovery_word_in_a_path_is_fine_outside_it_is_hidden():
    from orch.setup.steps import Word

    held = [Word("person")]
    assert redact_reason(PATH_MSG, held) == PATH_MSG
    assert redact_reason("the person said no", held) == HIDDEN_REASON
    assert redact_reason(PATH_MSG, ["person"]) == HIDDEN_REASON  # a plain str secret keeps the strict rule


def test_a_secret_embedded_in_a_path_is_still_hidden():
    from orch.setup.steps import Word, _Code

    code = "wasp milk patch into absorb example"
    held = [_Code(code), *map(Word, code.split())]
    assert redact_reason("no such file /tmp/absorb-example/x", held) == HIDDEN_REASON  # two words in a row
    assert redact_reason("no such file /tmp/wasp/milk/patch/x", held) == HIDDEN_REASON
    assert redact_reason(f"no such file /tmp/{code.replace(' ', '')}/x", held) == HIDDEN_REASON
    assert redact_reason("see http://h/person/x", [Word("person")]) == HIDDEN_REASON  # a URL is not a path
    assert redact_reason("x/person/y", [Word("person")]) == HIDDEN_REASON  # glued to a word: not a path


def test_a_passphrase_word_inside_a_path_is_hidden():
    # only recovery-code words are path-exempt; a passphrase is a plain str with the strict per-word rule
    assert redact_reason(PATH_MSG, ["person-config-person"]) == HIDDEN_REASON
    c, s = creator()
    c._passphrase = "person-quiet-meadow"
    assert c._safe(PATH_MSG) == HIDDEN_REASON


def test_two_distinct_code_words_anywhere_are_hidden_in_any_order():
    from orch.setup.steps import held_words

    held = held_words("wasp milk patch into absorb example mandate")
    assert redact_reason("see /a/absorb and then /b/mandate", held) == HIDDEN_REASON
    assert redact_reason("mandate ... far apart ... absorb", held) == HIDDEN_REASON
    assert redact_reason("only /a/absorb here", held) == "only /a/absorb here"


def test_short_ordinary_code_words_do_not_hide_a_real_message():
    from orch.setup.steps import held_words

    msg = "Open System Settings, General, Login Items & Extensions and allow orch, then try again"
    held = held_words("item login extend orch quiet")  # 4-5 letter words; two are whole tokens of the message
    assert redact_reason(msg, held) == msg
    assert redact_reason(msg, held_words("items extensions banana")) == HIDDEN_REASON  # a 6+ letter word still hides


def test_four_distinct_short_code_words_are_hidden_three_are_not():
    from orch.setup.steps import held_words

    held = held_words("item login extend orch allow again quiet")
    assert redact_reason("go to item, then login, extend it, and orch", held) == HIDDEN_REASON  # 4 short words
    three = "go to item, then login, and extend it"
    assert redact_reason(three, held) == three
    msg = "Open System Settings, General, Login Items & Extensions and allow orch, then try again"
    assert redact_reason(msg, held_words("zebra yonder login items extend")) == msg  # real message, pinned secrets
