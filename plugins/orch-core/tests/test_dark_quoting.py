"""Dark AI Factory: a prefix rule reads a command's words as the shell would. Metacharacters inside correctly quoted
text are plain text to the shell, so they no longer refuse a command; the split is checked against the real shells
(sh, bash, zsh when present) on many generated commands: whenever orch reads words, the shell reads the same words."""
import os
import random
import shutil
import subprocess

import pytest

from orch.core import dark_profile

ZSH_OPTS = ["/bin/zsh", "-f", "-o", "extendedglob", "-o", "rcquotes"]
SHELLS = [s for s in (["/bin/sh"], ["/bin/bash", "--norc", "--noprofile"], ["/bin/zsh", "-f"], ZSH_OPTS)
          if os.path.exists(s[0])]
PUNCT = ";&|<>()#$`\\!*?[]{}~'\" =-_.,:/@%+^"
BARE = "abcxyz019-_.,:/@%+"


def _piece(rng):
    k = rng.random()
    if k < 0.3:
        return "".join(rng.choice(BARE) for _ in range(rng.randint(1, 4)))
    if k < 0.55:  # single quoted: anything but the single quote itself
        return "'" + "".join(rng.choice(PUNCT + "ab") for _ in range(rng.randint(0, 5))).replace("'", "") + "'"
    if k < 0.85:  # double quoted, sometimes with an escaped quote or an active character
        body = "".join(rng.choice(PUNCT + "ab") for _ in range(rng.randint(0, 5))).replace('"', "")
        if rng.random() < 0.15:
            body += '\\"'
        return '"' + body + '"'
    if k < 0.95:
        return rng.choice(PUNCT)  # a bare metacharacter (or a stray quote)
    return rng.choice(["'", '"'])  # an unbalanced quote


def _command(rng):
    words = ["".join(_piece(rng) for _ in range(rng.randint(1, 3))) for _ in range(rng.randint(1, 4))]
    return " ".join(words)


def _shell_words(shell, commands):
    """The argv each command's words give in `shell`, one printf per line, run in an empty folder with an empty
    environment (nothing the commands could name exists there)."""
    script = "\n".join(f"printf '%s\\0' X {c}; printf '\\n'" for c in commands)
    with __import__("tempfile").TemporaryDirectory() as d:
        r = subprocess.run([*shell, "-c", script], capture_output=True, text=True, cwd=d, timeout=60,
                           env={"PATH": "/usr/bin:/bin"}, stdin=subprocess.DEVNULL)
    lines = r.stdout.split("\n")[:-1]
    assert len(lines) == len(commands), (shell, r.stderr[-500:])
    return [ln.split("\0")[1:-1] for ln in lines]


def _corpus(n=4000, seed=20261005):
    rng = random.Random(seed)
    return [_command(rng) for _ in range(n)]


@pytest.mark.skipif(not SHELLS, reason="no shell to compare with")
def test_quoted_words_split_exactly_as_the_real_shells_split_them():
    corpus = _corpus()
    read = [(c, dark_profile.simple_tokens(c)) for c in corpus]
    kept = [(c, w) for c, w in read if w is not None]
    # not vacuous: many commands are read, many of them with a metacharacter inside quotes
    assert len(kept) > 600
    assert sum(1 for c, _ in kept if any(m in c for m in ";&|<>()#")) > 200
    for shell in SHELLS:
        got = _shell_words(shell, [c for c, _ in kept])
        bad = [(c, w, g) for (c, w), g in zip(kept, got) if w != g]
        assert not bad, (shell, bad[:5])


@pytest.mark.parametrize("command,words", [
    ('orch task done T-2 T1 -m "Elephant data (WWF, IUCN; 2024)"',
     ["orch", "task", "done", "T-2", "T1", "-m", "Elephant data (WWF, IUCN; 2024)"]),
    ("orch log T-2 -m 'a | b && c > d # e $(x) `y` \\ !'",
     ["orch", "log", "T-2", "-m", "a | b && c > d # e $(x) `y` \\ !"]),
    ('git commit -m "T-3 add the <list> & #tags"', ["git", "commit", "-m", "T-3 add the <list> & #tags"]),
    ("orch log X -m ''", ["orch", "log", "X", "-m", ""]),
    ("orch log X -m 'a'\"(b)\"c", ["orch", "log", "X", "-m", "a(b)c"]),
    ("orch log X 'a#b' \"c^d\" 'e!'", ["orch", "log", "X", "a#b", "c^d", "e!"]),
])
def test_metacharacters_inside_quotes_are_plain_text(command, words):
    assert dark_profile.simple_tokens(command) == words


@pytest.mark.parametrize("command", [
    "orch show X; rm -rf y", "orch show X && orch claim X", "orch wait X | head", "orch wait X 2>&1",
    "orch show X > f", "orch show (X)", "orch log X -m \"$(id)\"", "orch log X -m \"`id`\"",
    'orch log X -m "a\\"b"', 'orch log X -m "hi!"', 'orch log X -m "${HOME}"', "orch log X -m 'open",
    'orch log X -m "open', "orch show X # a comment", "orch show X #", "orch log X -m 'a'; rm x",
    "orch log X -m \"a\"$(id)", "orch log X\n-m a",
    # zsh's extendedglob and rcquotes, and history: # ^ ! anywhere outside quotes, a quote right after a quote
    "orch log X a#b", "orch log X --#exec=x", "orch log X a^b", "orch log X ^x", "orch log X a!b", "! orch show X",
    "orch log X 'a''b'", "orch log X ''''", "orch log X 'a'''",
    # ANSI-C and locale quoting, and an empty quote before # or =
    "orch log X $'a\\nb'", 'orch log X $"a"', "orch log X ''#x", 'orch log X ""=x',
    # whitespace and control characters other than a plain space
    "orch log X\ta", "orch log X\ra", "orch log X a", "orch log X a", "orch log X a", "orch log\x00 X",
    "orch log X\x7f", "orch log X\x0ba", "orch log X\x0ca",
])
def test_active_text_still_refuses(command):
    assert dark_profile.simple_tokens(command) is None


def test_single_quoted_substitution_is_inert_and_matches(configure, human):
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": True})
    Ops(ws, human).set_factory_dark(True)
    dark_profile.add(ws, human, "prefix", "orch log")
    assert dark_profile.match(ws, "orch log L-0001 -m 'costs $(x) and `y`'") is not None
    assert dark_profile.match(ws, 'orch log L-0001 -m "the (WWF) list; done"') is not None
    for bad in ('orch log L-0001 -m "$(x)"', "orch log L-0001; orch log L-0002", "orch log L-0001 | cat",
                "orch log L-0001 && orch log L-0002"):
        assert dark_profile.match(ws, bad) is None, bad


def test_a_prefix_rule_itself_stays_plain_words(configure, human):
    from orch.errors import ValidationError
    ws = configure(factory={"enabled": True})
    with pytest.raises(ValidationError):
        dark_profile.add(ws, human, "prefix", "orch log 'a(b'")


def test_the_options_the_shells_differ_on_are_real():
    """Not vacuous: under zsh's rcquotes and extendedglob these really split differently from sh (in a folder holding
    a file named --exec=x), and orch refuses each of them."""
    if not os.path.exists(ZSH_OPTS[0]):
        pytest.skip("no zsh")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (__import__("pathlib").Path(d) / "--exec=x").write_text("", encoding="utf-8")
        for cmd in ("'a''b'", "--#exec=x", "^nothing"):
            run = lambda sh: subprocess.run([*sh, "-c", f"printf '%s\\0' X {cmd}"], capture_output=True,  # noqa: E731
                                            text=True, cwd=d, env={"PATH": "/usr/bin:/bin"}).stdout
            assert run(ZSH_OPTS) != run(["/bin/sh"]), cmd
            assert dark_profile.simple_tokens(f"orch log X {cmd}") is None
    assert shutil.which("sh")
