"""The Claude Code PreToolUse guard: what an agent's tool calls may not do in an orch workspace.

Best effort by design. It reads command text, not what a shell will finally run: shell functions and aliases,
variables and encoded text, scripts written first and run later, and interpreters can all get past string matching.
It keeps the common, one-command paths closed and gives clear reasons; the human-only checks in orch itself
(orch.actor, orch.core.lifecycle), the signed approval ledger (orch.core.ledger) and `orch check` do not depend on it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from orch.core.model import parse_ticket
from orch.core.protect import agent_wrote_ask, protected_changes as _protected_changes
from orch.errors import TicketParseError


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""


ALLOW = Decision(True)
_USE_ORCH = "ticket files and orchestrator/.state change only through the `orch` command (see the orch-tickets skill)"

_GIT = r"\bgit\b(?:\s+-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+))*\s+"
# git accepts any unambiguous prefix of a long option, so `--no-veri` is `--no-verify`.
_NO_VERIFY_OPT = r"--no-v(?:e(?:r(?:i(?:f(?:y)?)?)?)?)?"
_COMMIT = re.compile(_GIT + r"commit\b")
_PUSH = re.compile(_GIT + r"push\b")
_PUSH_DRY_RUN = re.compile(_GIT + r"push\b.*\s(?:--dry-run|-n)(?=\s|$)")
_NO_VERIFY_COMMIT = re.compile(_GIT + r"commit\b.*\s(?:" + _NO_VERIFY_OPT + r"|-n)(?=\s|$)")
_NO_VERIFY_PUSH = re.compile(_GIT + r"push\b.*\s" + _NO_VERIFY_OPT + r"(?=\s|$)")
_GIT_WORD = re.compile(r"\bgit\b")
_GIT_CONFIG = re.compile(_GIT + r"config\b")
_HOOKS_PATH_OVERRIDE = re.compile(r"(?i)\s-c\s*core\.hookspath\s*=")
_HOOKS_PATH_KEY = re.compile(r"(?i)\bcore\.hookspath\b")
_CONFIG_READ = re.compile(r"\sconfig\s+(?:\S+\s+)*?(?:--get(?:-all|-regexp)?|get|--list|-l)(?=\s|$)")
# `orch serve`, `uv run orch serve`, `python -m orch.cli serve`, `/path/to/orch --x serve`, ...
_SERVE = re.compile(r"\borch(?:\.cli)?\s+(?:-\S+\s+)*serve\b")
# A quoted string that is itself an `orch serve` command line (e.g. `script -c "orch serve"`), or a
# quoted path to the wrapper followed by `serve` (`"${CLAUDE_PLUGIN_ROOT}/bin/orch" serve`,
# `'/a b/bin/orch' serve`): the path may contain spaces, and a closing quote may follow `orch`.
_QUOTED_SERVE = re.compile(r"""['"]\s*(?:[^'"\n]*/)?(?:uv\s+run\s+|uvx\s+)?orch['"]?\s+(?:-\S+\s+)*serve\b""")
_SERVE_DENIED = "the dashboard is the human's; ask the user to open it"
# `orch addon install|update|trust|enable|disable|remove|rollback` in any form (`uv run orch`, `python -m orch.cli`,
# `orch --quiet addon ...`); `list` and `check` stay open to agents.
_ADMIN_VERBS = r"(?:install|update|trust|enable|disable|remove|rollback)\b"
_ADDON_ADMIN = re.compile(r"\borch(?:\.cli)?\s+(?:-\S+\s+)*addon\s+(?:-\S+\s+)*" + _ADMIN_VERBS)
# A quoted wrapper path ("${CLAUDE_PLUGIN_ROOT}/bin/orch" addon trust x): the quote must close right after `orch`,
# so a quoted sentence such as a commit message 'orch addon trust is human-only' is not mistaken for the command.
_QUOTED_ADDON_ADMIN = re.compile(r"""['"]\s*(?:[^'"\n]*/)?orch['"]\s+(?:-\S+\s+)*addon\s+(?:-\S+\s+)*""" + _ADMIN_VERBS)
_ADDON_ADMIN_DENIED = ("installing, updating, trusting, enabling, disabling, rolling back or removing addons is the "
                       "human's; ask the user to do it in their own terminal or in Workspace & addons")
# Human-only orch commands (#19): approve, answer, verdict, request-changes, reopen, close, `epic pause`, `permit
# grant|deny|revoke` (AI Factory), `dark profile add|remove|prune`, `factory dark on` and `factory release ...` (Dark AI
# Factory), and moves to a
# status only
# the human moves to. Agents never run them, in any spelling: `uv run orch`, `python -m orch.cli`, a wrapper path,
# `orch --json …`, inside `sh -c`/`eval`/heredocs (via _command_segments), or under a pty wrapper.
_HUMAN_VERBS = ("approve", "answer", "verdict", "request-changes", "reopen", "close", "ledger")
_HUMAN_TARGETS = ("backlog", "open", "in-progress", "done")
_HUMAN_VERB_RE = (r"(?:approve|answer|verdict|request-changes|reopen|close|ledger|epic\s+(?:-\S+\s+)*pause"
                  r"|permit\s+(?:-\S+\s+)*(?:grant|deny|revoke)"
                  r"|dark\s+(?:-\S+\s+)*profile\s+(?:-\S+\s+)*(?:add|remove|prune)"
                  r"|factory\s+(?:-\S+\s+)*dark\s+(?:-\S+\s+)*on"
                  r"|factory\s+(?:-\S+\s+)*release\s+(?:-\S+\s+)*(?:set|show|clear|retry)"
                  r"|factory\s+(?:-\S+\s+)*clones\s+(?:-\S+\s+)*(?:list|clean))(?![\w-])")
_HUMAN_MOVE_RE = r"move\s+(?:-\S+\s+)*\S+\s+(?:-\S+\s+)*(?:backlog|open|in-progress|done)(?![\w-])"
_HUMAN_CMD = re.compile(r"\borch(?:\.cli)?\s+(?:-\S+\s+)*(?:" + _HUMAN_VERB_RE + "|" + _HUMAN_MOVE_RE + ")")
_QUOTED_HUMAN_CMD = re.compile(r"""['"]\s*(?:[^'"\n]*/)?(?:uv\s+run\s+|uvx\s+)?orch(?:\.cli)?['"]?\s+(?:-\S+\s+)*(?:"""
                               + _HUMAN_VERB_RE + "|" + _HUMAN_MOVE_RE + ")")
# Code that drives orch from an interpreter: the word orch (not orch-core, not orch.core) and a human verb anywhere in
# the command, or a human Actor built by hand.
_ORCH_WORD = re.compile(r"(?<![\w-])orch(?:\.cli)?(?![\w.-])")
_HUMAN_VERB_WORD = re.compile(r"(?<![\w-])(?:approve|answer|verdict|request[-_]changes|reopen|ledger_adopt|ledger_repair|epic_pause"
                              r"|permit_(?:grant|deny|revoke)|add_from_request|set_factory_dark|set_recipe|clear_recipe)(?![\w-])")
_HUMAN_PY = re.compile(r"""\bActor\s*\(\s*(?:kind\s*=\s*)?['"]human['"]|\bhuman_actor\b|\brecord_approval\b""")
# Programs that give a command a pseudo-terminal (the TTY check of human-only actions) or type it into a terminal
# outside the agent's process tree.
_PTY_CMD = re.compile(r"(?:^|[\s;&|(`])(?:script|unbuffer|expect|socat|tmux|screen|osascript)(?=\s|$)")
_PTY_PY = re.compile(r"\b(?:pexpect|ptyprocess)\b|\bpty\.(?:spawn|fork|openpty)\b")


# Mission Control's terminals (issue #40) live on tmux's "orch" socket: an agent that could talk to it could read or
# type into another agent's session, or the human's. `-L orch`, `-Lorch` and a socket path ending in /orch.
_ORCH_TMUX = re.compile(r"\btmux\b(?:\s+-\S+)*?\s+(?:-L\s*orch\b|-S\s*\S*/orch\b)|\bTMUX=\S*/orch\b")
_ORCH_TMUX_DENIED = ("Mission Control's terminals (tmux -L orch) are the human's: they watch and type into them in the "
                     "dashboard; an agent does not reach another session through them")


# The AI Factory's own tmux server sits on a socket inside the permits folder. Best effort: a text guard cannot read
# bash the way bash does, so this makes the obvious routes fail and no more. A command that mentions tmux or screen
# anywhere (any case, quotes and backslashes removed) is refused unless it is one plain command (no expansion,
# substitution, grouping, redirection, comment, separator, glob or ANSI-C string, no mention of the permits folder) whose
# every socket argument is plain: -S an absolute, normalised path with no `..` outside the config dir (never a relative
# one: the working directory is not known to a later command), -L a plain name. Commands are also judged by what they
# resolve to: a working directory or a `cd`/`pushd` target at or below the config dir, and a listing of it, are refused.
# Known limits: a word written without a mention of tmux or screen by concatenation that uses none of the characters
# above, other languages building the string (perl, osascript, python), and a script file written and then run.
# A same-user process is not isolated from the socket by the OS.
_MUX_WORD = re.compile(r"tmux|screen")
_MUX_UNSURE = re.compile(r"[$`(){}<>#;&|!*?\[\]\n\r\x0b\x0c\u2028\u2029\x85]")
_MUX_FLAG = re.compile(r"-([A-Za-z0-9]*?)([LS])(.*)", re.S)
_MUX_ANSI_C = re.compile(r"\$'|\\[xuU0-7]")
_ORCH_TMUX_I = re.compile(_ORCH_TMUX.pattern, re.I)
_MUX_DENIED = ("a tmux or screen command the guard cannot show plain (variables, escapes, substitutions, separators, a "
               "relative or unusual socket, or the permits folder) can reach the AI Factory's sessions, which are the "
               "human's")
_STATE_DENIED = ("the orch config dir (the ledger and the permits folder with the AI Factory's records and sockets) is "
                 "the human's: agents do not work in it, change into it or list it")
_LISTERS = ("ls", "dir", "vdir", "find", "stat", "du", "tree", "exa", "eza", "lsd", "fd", "ncdu", "realpath", "readlink")
_RECURSIVE = ("find", "du", "tree", "fd", "ncdu")


class _Bound(Exception):
    """A limit of the resolution rules was hit (command length, glob matches, path depth, time): the answer is a deny."""


MAX_CMD, MAX_GLOB, MAX_PARTS, MAX_CDS, MAX_WORDS, BUDGET_S = 200_000, 500, 128, 64, 20_000, 3.0
_BOUND_DENIED = ("the guard hit one of its limits (a very long command, a huge glob, a very deep path or too much time) "
                 "while checking a path near the orch config dir, and refuses what it cannot finish checking")  # bounds only


class _Budget:
    def __init__(self) -> None:
        import time
        self.t0, self.n = time.monotonic(), 0

    def tick(self) -> None:
        import time
        self.n += 1
        if self.n > 60_000 or time.monotonic() - self.t0 > BUDGET_S:
            raise _Bound("time")


def _state_dir() -> Path | None:
    from orch.core.ledger import base_dir
    try:
        return Path(base_dir()).resolve()
    except (OSError, RuntimeError):
        return None


def _expand(raw: str, extra: dict | None = None) -> str:
    """`raw` with ~ and the variables a shell would fill in: the hook's environment, the orch state variable (defaulting
    to the real folder: an agent's shell has it even where the hook does not), and `extra` (assignments made in the
    command itself, PWD and OLDPWD as the simulated `cd` chain has them)."""
    from orch.core.ledger import base_dir
    env = {**os.environ, "ORCH_STATE_DIR": os.environ.get("ORCH_STATE_DIR") or str(base_dir()), **(extra or {})}
    return re.sub(r"\$(?:\{(\w+)\}|(\w+))", lambda m: env.get(m.group(1) or m.group(2), m.group(0)),
                  os.path.expanduser(raw))


def _real(path: str) -> Path:
    """The realpath of `path`, once. A link loop (or anything else the system refuses) raises _Bound."""
    import errno
    p = os.path.realpath(path)
    try:
        os.stat(path)
    except OSError as e:
        if e.errno not in (errno.ENOENT, errno.ENOTDIR):
            raise _Bound("path") from e
    return Path(p)


def _within(p: Path, base: Path) -> bool:
    return p == base or base in p.parents


def _link_into(full: str, base: Path, bud: _Budget) -> bool:
    """Some component of the path `full`, taken one by one without resolving the rest, is a symlink that leads into
    `base`. Such a path is refused even if the whole of it resolves elsewhere: the link can be swapped between this
    check and the use, so only the path as written is judged, and never trusted because of where it points today."""
    cur, n = "/", 0
    for comp in full.split("/"):
        bud.tick()
        if comp in ("", "."):
            continue
        n += 1
        if n > MAX_PARTS:
            raise _Bound("depth")
        if comp == "..":
            cur = os.path.dirname(cur)
            continue
        cur = os.path.join(cur, comp)
        if os.path.islink(cur) and _within(_real(cur), base):
            return True
    return False


def _resolved(cur: str, raw: str, bud: _Budget, extra: dict | None = None) -> list[Path]:
    """Where `raw` (a path as written, quotes removed) points from `cur`: every glob match, else the literal path. One
    realpath each, compared as it comes. Raises _Bound past the limits."""
    import glob
    from itertools import islice
    text = _expand(raw.strip("'\""), extra)
    if len(text) > 4096 or text.count("/") > MAX_PARTS:
        raise _Bound("long")
    full = text if os.path.isabs(text) else os.path.join(cur, text)
    hits = []
    if re.search(r"[*?\[]", text):
        hits = list(islice(glob.iglob(full), MAX_GLOB + 1))
        if len(hits) > MAX_GLOB:
            raise _Bound("glob")
    out = []
    for h in hits or [full]:
        bud.tick()
        try:
            out.append(_real(h))
        except (RuntimeError, ValueError) as e:
            raise _Bound("path") from e
    return out


_ASSIGN = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.S)
_KEYWORDS = {"then", "do", "else", "elif", "if", "while", "until", "!", "time", "{", "}", "(", ")", "&&", "||"}
_SENSITIVE = ("permits", "sessions", "armed", "runs", "children", "requests", "used", "ledger*", "factory-command*",
              "factory-release*", "release-records", "release-repos", "child-clones", "nudges", "early-ends", "tmux", "tmux.name", "remote-humans*", "launch.json")
# real names a glob could stand for, to ask "can this pattern reach one of them"
_SENSITIVE_NAMES = ("permits", "sessions", "armed", "runs", "children", "requests", "used", "ledger.key", "ledger.jsonl",
                    "ledger.head", "ledger.lock", "factory-command.json", "factory-release.json",
                    "factory-release.json.lock", "release-records", "release-repos", "child-clones", "nudges", "early-ends", "tmux", "tmux.name", "remote-humans.json",
                    "launch.json")
_READERS = {"cat", "less", "more", "head", "tail", "cp", "mv", "tar", "zip", "rsync", "ls", "find", "rg", "du", "tree",
            "bat", "wc", "xargs", "dir", "vdir"}
_UNKNOWN_DENIED = ("after a cd to a place the guard cannot work out, this command names something that may be in the "
                   "orch config dir (permits, ledger, sessions, tmux, ...): cd to a plain path first, or use an "
                   "absolute path")


def _prep(cmd: str) -> str:
    """The command with line continuations joined and every kind of line break a newline."""
    cmd = re.sub(r"\\\r?\n", "", cmd)
    return re.sub(r"[\r\x0b\x0c  \x85]", "\n", cmd)


def _toks(seg: str) -> list[str]:
    import shlex
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.replace("'", " ").replace('"', " ").split()


def _command(seg: str) -> tuple[list[str], dict]:
    """(the words of simple command `seg` from its command word on, the assignments before it)."""
    toks = [t for t in _toks(seg)]
    assigns: dict = {}
    i = 0
    while i < len(toks):
        t = toks[i].lstrip("({")
        if not t:
            i += 1
            continue
        m = _ASSIGN.match(t)
        if m and not t.startswith("-"):
            assigns[m.group(1)] = m.group(2)
            i += 1
        elif t in _KEYWORDS:
            i += 1
        else:
            break
    words = toks[i:]
    if words:
        words[0] = words[0].lstrip("({")
    return words, assigns


def _sensitive_component(comp: str, first: bool, reads: bool) -> bool:
    """A path component that is, or can stand for, a name in the orch config dir. A bare glob counts only as the first
    component and only when the command reads or lists; an extension glob (`*.md`) never counts."""
    import fnmatch
    if not re.search(r"[*?\[]", comp):
        return any(fnmatch.fnmatchcase(comp, pat) for pat in _SENSITIVE)
    if re.fullmatch(r"\*+|\?\**|\*\?+|\*+/?", comp):
        return first and reads
    if re.fullmatch(r"\*\.[A-Za-z0-9]+", comp):
        return False
    return any(fnmatch.fnmatchcase(n, comp) for n in _SENSITIVE_NAMES)


def _unknown_word(word: str, reads: bool) -> bool:
    if word.startswith(("/", "~")):
        return False  # absolute: judged by the same rules as ever
    comps = [c for c in word.split("/") if c not in ("", ".", "..")]
    return any(_sensitive_component(c, i == 0, reads) for i, c in enumerate(comps))


_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "eval"}
_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'")


def _with_nested(segs: list[str]) -> list[str]:
    """`segs` plus what the segment list alone does not show: the text of an ANSI-C string given to a shell or eval
    (decoded), and the arguments of an echo or printf when a shell that reads standard input is in the line."""
    import codecs
    out = list(segs)
    bare = False
    for seg in segs:
        words, _ = _command(seg)
        prog = os.path.basename(words[0]) if words else ""
        if prog in _SHELLS:
            if not any(a.startswith("-") and "c" in a for a in words[1:]) and prog != "eval":
                bare = bare or len(words) == 1
            for m in _ANSI_C.finditer(seg):
                try:
                    inner = codecs.decode(m.group(1).encode("latin-1", "replace"), "unicode_escape")
                except (UnicodeDecodeError, ValueError):
                    continue
                out.extend(_command_segments(inner))
    if bare:
        for seg in segs:
            words, _ = _command(seg)
            if words and os.path.basename(words[0]) in ("echo", "printf"):
                out.extend(_command_segments(" ".join(words[1:])))
    return out


def _touches_state_dir(ws, cmd: str, cwd):
    """False, True (a cd, a path or a listing resolves into the config dir), or "unknown" (the working directory is
    not known after a cd the guard cannot work out, and a word names something that may be in the config dir). Every
    command of the line is looked at in order: quoted text that is not a command is not one (`echo "cd x"`), but the
    payloads of `sh -c`, `eval` and substitutions, and heredoc bodies that are code, are. Raises _Bound when a limit
    is hit, so that the caller denies."""
    base = _state_dir()
    if base is None:
        return False
    if len(cmd) > MAX_CMD:
        raise _Bound("length")  # whatever it holds: nothing too long to check is let through
    bud = _Budget()
    cur = str(cwd) if cwd else os.getcwd()
    for start in {cur, os.getcwd()}:  # the hook's cwd and this process's own
        try:
            if _within(_real(start), base) or _link_into(start, base, bud):
                return True
        except (OSError, RuntimeError, ValueError):
            return True
    segs = _with_nested(_command_segments(_prep(cmd)))
    if len(segs) > MAX_WORDS:
        raise _Bound("words")
    try:
        root = Path(ws.root).resolve()
    except (OSError, RuntimeError):
        root = None
    extra: dict = {}
    unknown, cdpath_set = False, False
    old = os.environ.get("OLDPWD", "")
    cdpath = [d for d in os.environ.get("CDPATH", "").split(os.pathsep) if d]
    where, cds, seen = [cur], 0, 0
    for seg in segs:
        bud.tick()
        words, assigns = _command(seg)
        extra["PWD"], extra["OLDPWD"] = cur, old
        for k, v in assigns.items():
            extra[k] = _expand(v, extra)
            cdpath_set = cdpath_set or k == "CDPATH"
        if not words:
            continue
        prog, args = os.path.basename(words[0]), words[1:]
        cdpath_set = cdpath_set or any(a.startswith("CDPATH=") for a in args)
        if prog in ("cd", "pushd", "chdir"):
            cds += 1
            if cds > MAX_CDS:
                raise _Bound("cds")
            ops = [a for a in args if a != "--" and not (a.startswith("-") and a != "-")]
            raw = (ops[0] if ops else "~").rstrip(")};")
            extra["PWD"], extra["OLDPWD"] = cur, old
            target = _expand(raw, extra)
            if target == "-":
                target = old or cur
            if re.search(r"[$`]", target) or cdpath_set:
                unknown = True  # a place the guard cannot work out: allowed, but the working directory is unknown now
                continue
            for c in ([cur, *cdpath] if not os.path.isabs(target) else [cur]):
                if _link_into(target if os.path.isabs(target) else os.path.join(c, target), base, bud):
                    return True
            hits = _resolved(cur, target, bud, extra)
            if any(_within(h, base) for h in hits):
                return True
            for d in cdpath if not os.path.isabs(target) else []:
                if any(_within(h, base) for h in _resolved(d, target, bud, extra)):
                    return True
            old, cur = cur, str(hits[0])
            where.append(cur)
            continue
        reads = prog in _READERS or (prog in ("grep", "egrep", "fgrep") and any(
            a.startswith("-") and not a.startswith("--") and ("r" in a or "R" in a) for a in args))
        recursive = prog in _RECURSIVE or (prog == "ls" and any(
            a.startswith("-") and not a.startswith("--") and "R" in a for a in args)) or "--recursive" in args
        for w in (a.rstrip(")};") if a not in (")", "}") else a for a in args):
            seen += 1
            if seen > MAX_WORDS:
                raise _Bound("words")
            if unknown and _unknown_word(w, reads):
                return "unknown"
            if "/" not in w and not w.startswith(("~", ".", "$")) and prog not in _LISTERS:
                continue
            if w.startswith("-") and prog in _LISTERS:
                continue
            full = _expand(w, extra)
            if len(full) > 4096 or full.count("/") > MAX_PARTS:
                raise _Bound("long")
            if unknown and not os.path.isabs(full) and not full.startswith("~"):
                continue  # relative to a place we do not know: the sensitive-name rule above is all there is
            if re.search(r"[$`]", full):
                continue  # an expansion the guard cannot follow
            joined = full if os.path.isabs(full) else os.path.join(cur, full)
            glob_at = re.search(r"[*?\[]", joined)
            if glob_at:
                near = _real(os.path.dirname(joined[:glob_at.start()]) or "/")
                if _within(near, base) or near in base.parents:  # a glob that can reach the config dir: expand, bounded
                    if any(_within(h, base) for h in _resolved(cur, w, bud, extra)):
                        return True
                continue
            if _link_into(joined, base, bud) or _within(_real(joined), base):
                return True
            if prog in _LISTERS:
                for c in where:
                    for h in _resolved(c, w, bud, extra):
                        if _within(h, base):
                            return True
                        if recursive and h in base.parents and not (root is not None and (h == root or h in root.parents)):
                            return True
    return False


_WRAPPERS = {"sh", "bash", "zsh", "dash", "ksh", "eval", "env", "command", "exec", "nohup", "time", "sudo", "doas",
             "xargs", "builtin", "source", ".", "ssh", "script", "unbuffer", "expect", "watch"}
_DEFINERS = {"alias", "function", "declare", "typeset", "export", "local", "readonly", "trap"}


def _mux_segment_risky(seg: str) -> bool:
    """One tmux or screen command (or a wrapper carrying one), judged as plain or not."""
    plain = seg.replace("'", "").replace('"', "").replace("\\", "")
    text = plain.lower()
    if "permits" in text or _ORCH_TMUX_I.search(text):
        return True
    if re.search(r"[$`]", plain) or _MUX_UNSURE.search(_unquoted(seg).replace("$", "")):
        return True
    base = _state_dir()
    toks = plain.split()
    for i, tok in enumerate(toks):
        m = _MUX_FLAG.fullmatch(tok)
        if not m:
            continue
        before, letter, rest = m.groups()
        if before:  # a cluster such as -CCS: the value belongs to a flag we cannot place
            return True
        value = rest if rest else (toks[i + 1] if i + 1 < len(toks) else "")
        if letter == "L":
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                return True
        else:
            if not re.fullmatch(r"/[A-Za-z0-9_./-]*", value) or ".." in value.split("/") or "//" in value:
                return True
            if base is not None:
                try:
                    if _within(_real(value), base) or _link_into(value, base, _Budget()):
                        return True
                except (OSError, RuntimeError, ValueError, _Bound):
                    return True
    return False


def _mux_risky(cmd: str) -> bool:
    """A tmux or screen command the guard cannot show plain. Only the command word and its own arguments count: a
    `grep tmux`, a heredoc body, a quoted message or a `#` inside quotes is not one. Best effort."""
    if len(cmd) > MAX_CMD and re.search(r"tmux|screen", cmd, re.I):
        raise _Bound("length")
    flat = _prep(cmd)
    whole = re.search(r"tmux|screen", flat, re.I)
    if _MUX_ANSI_C.search(flat) and _MUX_FLAG.search(flat):
        return True
    if not whole:
        return False
    if whole and re.search(r"(?<![\w-])function\s|\w\s*\(\)|(?<![\w-])alias\s", flat):
        return True  # a function or an alias that may wrap the command: not provable
    for seg in _command_segments(flat):
        words, _ = _command(seg)
        if not words:
            continue
        prog = os.path.basename(words[0])
        if prog in ("tmux", "screen"):
            if _mux_segment_risky(seg):
                return True
        elif (prog in _WRAPPERS or prog in _DEFINERS) and re.search(r"tmux|screen", seg, re.I):
            if prog in _DEFINERS or _mux_segment_risky(seg):
                return True
        elif re.match(r"[$`]", words[0]) and whole:  # a command word the guard cannot read, and tmux is mentioned
            if _mux_segment_risky(seg) or re.search(r"(?<![\w-])-[LS]", seg):
                return True
    return False


def _pty_wrapped(text: str) -> bool:
    """A pty or terminal wrapper as a command word (quoted text aside), or Python's pty helpers."""
    return bool(_PTY_CMD.search(_unquoted(text)) or _PTY_PY.search(text))


def _drives_orch_as_human(cmd: str, code: str) -> bool:
    """Interpreter or pty-wrapped code that runs a human-only orch command, or builds a human actor. The orch word
    and the verb must sit in one simple command, one shell payload or one heredoc body that is code, so a test run
    such as `python -m pytest -k approve && orch show L-1` is not mistaken for it."""
    if not (_HUMAN_INTERP.search(code) or _pty_wrapped(code)):
        return False
    if _HUMAN_PY.search(code):
        return True
    main, docs = _split_heredocs(cmd)
    units = _command_segments(cmd) + [d.body for d in docs if not _is_data_heredoc(main, d)]
    return any((_ORCH_WORD.search(u) and (_HUMAN_VERB_WORD.search(u) or _pty_wrapped(u)))
               or (_ORCH_MODULE.search(u) and (_HUMAN_VERB_WORD.search(u) or _APP_HUMAN.search(u)
                                               or _HUMAN_ARGV.search(u)))
               for u in units)


# Code that names an orch module (`orch.cli`, `orch.core.ops`, which _ORCH_WORD leaves out on purpose) together with a
# human verb word, a human-only argv list handed to the CLI app (`app(["factory", "dark", "on"])`), or a human-only
# AI Factory subcommand on the same command line (`python -c 'from orch.cli import app; app()' factory dark on`).
_ORCH_MODULE = re.compile(r"(?<![\w-])orch\.(?:cli|core)\b")
_Q = r"""['"]"""
_APP_HUMAN = re.compile(
    _Q + r"permit" + _Q + r"\s*,\s*" + _Q + r"(?:grant|deny|revoke)" + _Q
    + r"|" + _Q + r"dark" + _Q + r"\s*,\s*" + _Q + r"profile" + _Q + r"\s*,\s*" + _Q + r"(?:add|remove|prune)" + _Q
    + r"|" + _Q + r"factory" + _Q + r"\s*,\s*" + _Q + r"dark" + _Q + r"\s*,\s*" + _Q + r"on" + _Q
    + r"|" + _Q + r"factory" + _Q + r"\s*,\s*" + _Q + r"release" + _Q
    + r"|" + _Q + r"factory" + _Q + r"\s*,\s*" + _Q + r"clones" + _Q
    + r"|\[\s*" + _Q + r"(?:approve|answer|verdict|request-changes|reopen|close|ledger)" + _Q)
_HUMAN_ARGV = re.compile(r"(?:^|\s)(?:permit\s+(?:-\S+\s+)*(?:grant|deny|revoke)"
                         r"|dark\s+(?:-\S+\s+)*profile\s+(?:-\S+\s+)*(?:add|remove|prune)"
                         r"|factory\s+(?:-\S+\s+)*dark\s+(?:-\S+\s+)*on"
                         r"|factory\s+(?:-\S+\s+)*release\s+(?:-\S+\s+)*(?:set|show|clear|retry)"
                  r"|factory\s+(?:-\S+\s+)*clones\s+(?:-\S+\s+)*(?:list|clean))(?![\w-])")
_HUMAN_ONLY_DENIED = ("approving, answering, giving verdicts, requesting changes, adopting into the ledger, granting "
                      "permissions, changing the Dark profile or the release recipe and moving a "
                      "ticket to backlog, open, in-progress or done are the human's: ask the user to do it in their own "
                      "terminal or the dashboard")
# The harness markers orch reads to tell an agent from a human (orch.actor): an agent does not strip or blank them.
_HARNESS_VARS = r"(?:CLAUDECODE|CLAUDE_CODE_\w+|CLAUDE_PID|ORCH_HARNESS|AI_AGENT|CODEX_\w+|GEMINI_CLI)"
# `env` as a command (not `--env` of another tool): `-i`/`-`/`--ignore-environment` among its leading options, or
# `-u`/`--unset` of a harness marker; `unset` of a harness marker.
_ENV_STRIP = re.compile(
    r"(?<![\w-])env(?:\s+(?:-u\s*\S+|--unset[=\s]\S+|-\S+|[A-Za-z_]\w*=\S*))*?"
    r"\s+(?:-[A-Za-z0-9]*i[A-Za-z0-9]*|--ignore-environment|-)(?=\s|$)"
    r"|(?<![\w-])env(?:\s+(?:-\S+|[A-Za-z_]\w*=\S*|\S+(?=\s+-)))*?\s+(?:-u\s*|--unset[=\s]+)" + _HARNESS_VARS + r"\b"
    r"|(?<![\w-])unset\b[^;&|\n]*\b" + _HARNESS_VARS + r"\b")
_ENV_BLANK = re.compile(r"(?:^|[\s;&|(])(?:(?:export|declare|typeset|local|readonly)(?:\s+-\w+)*\s+)?"
                        + _HARNESS_VARS + r"""=(?:''|""|(?=\s|$|[;&|)]))""")
_ENV_DENIED = ("changing the environment to remove or blank the agent harness markers (CLAUDECODE, CLAUDE_CODE_*, "
               "ORCH_HARNESS, ...) is not allowed: orch uses them to keep human-only actions with the human")


def _human_only_tokens(seg: str) -> bool:
    """The simple command, split as the shell would (quotes and escapes resolved), runs an orch program (`orch`,
    `…/bin/orch`, `-m orch.cli`) with a human-only subcommand. Catches spellings such as `o''rch approve`."""
    import shlex
    try:
        words = shlex.split(seg, comments=True, posix=True)
    except ValueError:
        return False
    for k, word in enumerate(words):
        if re.split(r"[/\\]", word)[-1] not in ("orch", "orch.cli"):
            continue
        rest = [w for w in words[k + 1:] if not w.startswith("-")]
        if rest and rest[0] in _HUMAN_VERBS:
            return True
        if len(rest) >= 2 and rest[0] == "epic" and rest[1] == "pause":
            return True
        if len(rest) >= 2 and rest[0] == "permit" and rest[1] in ("grant", "deny", "revoke"):
            return True
        if len(rest) >= 3 and rest[0] == "dark" and rest[1] == "profile" and rest[2] in ("add", "remove", "prune"):
            return True
        if len(rest) >= 3 and rest[0] == "factory" and rest[1] == "dark" and rest[2] == "on":
            return True
        if len(rest) >= 2 and rest[0] == "factory" and rest[1] in ("release", "clones"):
            return True
        if len(rest) >= 3 and rest[0] == "move" and rest[2] in _HUMAN_TARGETS:
            return True
    return False


# Interpreters whose code can run orch (for _drives_orch_as_human).
_HUMAN_INTERP = re.compile(r"\b(?:python[0-9.]*|perl|ruby|node|deno|bun|php|lua)\b|<<")
# `orch $X …`, `orch "$(…)"`: a subcommand the guard cannot read. Agents name their orch subcommands.
_ORCH_DYNAMIC = re.compile(r"\borch(?:\.cli)?\s+(?:-\S+\s+)*['\"]?(?:\$|`)")
# `xargs orch` / `xargs -I{} orch …`: orch as the command of xargs; denied unless a literal subcommand that is not
# human-only follows (`xargs -n1 orch show`).
_XARGS_ORCH = re.compile(r"\bxargs(?:\s+(?:-\S+|\{\}|\d+))*\s+(?:\S*/)?orch(?:\.cli)?(?![\w.-])((?:\s+-\S+)*)\s*(\S*)")
# Decoding text and running it: a decoder whose output is piped into a shell or interpreter, or decoded inside a
# `$( … )` that eval/source/`sh -c` runs. A decoder alone (`base64 -d x > run.sh`) is fine.
_DECODE = r"(?:\bbase64\b[^;&|\n]*\s(?:-d|-D|--decode)\b|\bxxd\b[^;&|\n]*\s-r\b|\bopenssl\s+(?:base64|enc)\b[^;&|\n]*\s-d\b)"
_RUNNER_WORD = (r"(?:\S*/)?(?:(?:ba|da|k|z|fi|pw)?sh|python[0-9.]*|node|perl|ruby|php|osascript|eval|source"
                r"|busybox\s+(?:ba|a)?sh)(?=\s|$|[);])")
# Wrappers that run the next word as the command (`env sh`, `sudo -u root sh`, `nice -n 5 sh`, `timeout 5 sh`): an
# option may take one value (regex backtracking keeps the runner from being swallowed as that value).
_WRAP = (r"(?:(?:sudo|env|command|exec|nice|nohup|time|stdbuf|timeout|setsid|doas|ionice|chrt|script|unbuffer)"
         r"(?:\s+(?:-\S+(?:\s+(?![-|;&])\S+)?|\d+\S*|\w+=\S*))*\s+)*")
_RUN_CMD = r"[({]?\s*" + _WRAP + r"(?:xargs\b(?:\s+(?:-\S+|\{\}|\d+))*\s+)?" + _RUNNER_WORD
_RUNS_PAYLOAD = (r"(?:\beval|\bsource|(?<![\w.-])\.|(?<![\w./-])" + _WRAP
                 + r"(?:\S*/)?(?:(?:ba|da|k|z|fi|pw)?sh|python[0-9.]*|node|perl|ruby|php|busybox\s+(?:ba|a)?sh)"
                 + r"(?:\s+-\S+)*)")
_DECODED_RUN = re.compile(
    _DECODE + r"[^;&\n]*?\|\s*" + _RUN_CMD                                   # decode | sh, | (sh), | env sh, | xargs sh
    + r"|" + _DECODE + r"[^;&\n]*?\|\s*[({][^)}]*?(?<![\w./-])" + _WRAP + _RUNNER_WORD  # | ( cd x && sh ), | { …; sh; }
    + r"|" + _DECODE + r"""[^;&\n]*?\|\s*[gmn]?awk\b[^|;&\n]*\bsystem\b"""   # decode | awk '{system($0)}'
    + r"|" + _RUNS_PAYLOAD + r"""\s+["']?\$\([^)]*?""" + _DECODE             # eval "$(… decode …)", sh -c "$(…)"
    + r"|" + _RUNS_PAYLOAD + r"""\s+["']?`[^`]*?""" + _DECODE                 # eval "`… decode …`"
    + r"|" + _RUNS_PAYLOAD + r"\s+(?:<\s*)?<\(\s*[^)]*?" + _DECODE)          # sh <(…), bash < <(…), source <(…)
_DECODED_DENIED = "decoding text and running it as commands is not allowed for agents; run the command itself"
# `export -n CLAUDECODE` (un-export), `exec -c …` (run with an empty environment).
_ENV_UNEXPORT = re.compile(r"\bexport\s+(?:-\w*\s+)*-\w*n\w*\s+(?:\S+\s+)*?" + _HARNESS_VARS + r"\b"
                           r"|(?<![\w-])exec\s+(?:-\w*\s+)*-\w*c\w*(?=\s|$)")


def _runs_human_only(seg: str, plain: str) -> bool:
    return bool(_HUMAN_CMD.search(plain) or _QUOTED_HUMAN_CMD.search(seg) or _human_only_tokens(seg)
                or _ORCH_DYNAMIC.search(seg))


def _strips_harness_env(seg: str, plain: str) -> bool:
    return bool(_ENV_STRIP.search(plain) or _ENV_BLANK.search(seg) or _ENV_UNEXPORT.search(plain))


# The user addon files, however the config dir is spelled: ~/.config/orch/, $HOME/.config/orch/, $XDG_CONFIG_HOME/orch/.
_USER_ADDON_REL = r"orch[/\\](?:addons\.json|workspaces\.json|addons(?:[/\\]|\b))"
_USER_ADDON_FORMS = re.compile(r"(?:\.config|\$\{?XDG_CONFIG_HOME\}?)[/\\]" + _USER_ADDON_REL)
# Python that reaches the admin functions directly (`python -c "from orch.addons import manage; ..."`, `uv run python`,
# a heredoc script): manage refuses inside a harness anyway; this keeps an agent from scripting around that check.
_INTERPRETER = re.compile(r"\bpython[0-9.]*\b|<<")
_ADMIN_PY = re.compile(
    r"\borch\.addons\.(?:manage|userfiles)\b"
    r"|\bfrom\s+orch\.addons\s+import\b[^;\n]*\b(?:manage|userfiles)\b"
    r"|\b(?:record_trust|set_enabled)\b")
# The orch config dir reached through `cd` or a variable (`cd ~/.config/orch && echo {} > addons.json`,
# `D=~/.config/orch; ... > "$D/addons.json"`): a config-dir spelling plus an addon file name, in a writing command.
_CONFIG_BASE = re.compile(r"\.config(?![\w.-])|XDG_CONFIG_HOME")
_ADDON_FILE_NAME = re.compile(r"\b(?:addons|workspaces\.json)\b")
# remote-humans.json holds the phone pairing keys: agents never read or write it, by any tool (Task 7). Best effort
# for Bash: the file named in any spelling, the orch modules that read it, a glob or a recursive read of the config dir.
_REMOTE_KEYS = re.compile(r"(?i)remote[-_]?humans")
_CONFIG_SECRETS_DENIED = ("the orch config dir holds the phone pairing keys (remote-humans.json) and the approval "
                          "ledger with its signing key; agents do not read or list it wholesale")
_REMOTE_DENIED = ("remote-humans.json holds the phone pairing keys; only the human pairs or revokes phones, "
                  "in Mission Control → Workspace & addons → Phones")
# The approval ledger and its signing key (orch.core.ledger), in the orch config dir: the human's record of approvals.
# Agents never read or write them, by any tool; best effort for Bash, as for the pairing keys.
# The AI Factory's permit records beside it (orch.core.permits: request bodies and the markers that use up a once
# grant) are protected the same way: removing a marker would revive a used grant. The Dark profile's module
# (orch.core.dark_profile) is driven from code no more than the permits module.
_LEDGER = re.compile(r"(?i)\bledger\.(?:key|jsonl|head|lock)\b|orch[/\\]+(?:ledger|permits)\b|ORCH_STATE_DIR\}?[/\\]+(?:ledger|permits)\b"
                     r"|\bpermits[/\\]+(?:used|requests|children|sessions|armed|runs|factory-command|factory-release"
                     r"|release-records|release-repos|child-clones|nudges|early-ends|tmux)\b"
                     r"|\borch\.core\.(?:ledger|permits|dark_profile|factory_release|factory_clones)\b"
                     r"|\bfrom\s+orch\.core\s+import\b[^;\n]*\b(?:ledger|permits|dark_profile|factory_release|factory_clones)\b")
_LEDGER_DENIED = ("the approval ledger, its key and the permit records beside it are the human's signed record of "
                  "decisions; agents do not read or write them")
_REMOTE_PY = re.compile(r"\borch\.remote\b|\bfrom\s+orch\s+import\b[^;\n]*\bremote\b")
_CONFIG_DIR_FORMS = r"(?:\.config|\$\{?XDG_CONFIG_HOME\}?)[/\\]orch|\$\{?ORCH_STATE_DIR\}?"
_DIR_READER = re.compile(r"\b(?:e|f)?grep\b[^;&|\n]*\s(?:-\w*[rR]|--(?:dereference-)?recursive\b)"
                         r"|\b(?:rg|ag|ack|tar|zip|7z|rsync|scp|find|xargs|ditto|pax)\b"
                         r"|\bcp\s+(?:-\w+\s+)*-\w*[rRa]|\bcp\b[^;&|\n]*\s--(?:recursive|archive)\b")
_GLOB = re.compile(r"[*?\[]")
_REVIEW = re.compile(r"\b(?:gh\s+pr\s+create|glab\s+mr\s+create|az\s+repos\s+pr\s+create)\b")
_STATE_PATH = r"orchestrator[/\\]tickets\b|(?:orchestrator[/\\])?(?:tickets[/\\](?:backlog|open|in-progress|waiting|testing|done|INDEX\.md)|\.state)\b"
_STATE = re.compile(_STATE_PATH)
# `cd DIR`, `cd -- DIR`, `cd -P DIR`, `pushd DIR`: options before the target are skipped.
_CD = re.compile(r'\b(?:cd|pushd)\s+(?:-\S*\s+)*(?:"([^"]*)"|\'([^\']*)\'|(\S+))')
_WRITE_TOOL = re.compile(r"(?:^|[\s;&|(`'\"])(?:mv|rm|cp|tee|truncate|touch|git\s+mv|git\s+rm|sed\s+(?:-\w*i|--in-place)|perl\s+-\w*i)\b")
_OTHER_WRITE = re.compile(
    r"\bfind\b[^;&|\n]*\s-delete\b"
    r"|" + _GIT + r"checkout\b[^;&|\n]*\s--(?=\s|$)"
    r"|" + _GIT + r"restore\b"
    r"|\bdd\b[^;&|\n]*\bof="
)
# Any real output redirect (not to /dev/null, not a bare fd dup like 2>&1 or >&2). Checked against
# the command with quoted substrings blanked out, so a literal ">" inside quotes (e.g. `grep ">"
# file`) is not mistaken for a shell redirect.
_OUTPUT_REDIRECT = re.compile(r"(?<![0-9&])>{1,2}(?!\s*(?:/dev/null\b|&))")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_INTERP_WRITE = re.compile(r"\b(?:python3?|perl|ruby|node)\s+-[ce]\b")
# `sh -c "..."`, `bash -lc '...'`, `eval "..."`: the quoted payload is itself a command line.
_SHELL_PAYLOAD = re.compile(
    r"(?:\b(?:ba|da|k|z)?sh\s+(?:-\w+\s+)*-\w*c\s+|\beval\s+)(?:'([^']*)'|\"((?:\\.|[^\"\\])*)\")"
)


def _unquoted(cmd: str) -> str:
    """`cmd` with the content of every quoted string blanked (`'…'` and `"…"` become `''`), so quoted text such as a
    commit message is not read as a command. A backslash-escaped quote outside quotes is a literal character, not
    the start of a quoted string (the shell reads `don\'t` that way too)."""
    out, i, n = [], 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\" and i + 1 < n:
            out.append(cmd[i:i + 2])
            i += 2
        elif cmd.startswith("$(", i):  # its own quoting context: kept visible, checked as a payload too
            end = _sub_end(cmd, i + 2)
            out.append(cmd[i:end])
            i = end
        elif c == "'":
            end = cmd.find("'", i + 1)
            if end == -1:
                out.append(cmd[i:])  # unterminated: keep it visible
                break
            out.append("''")
            i = end + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                if cmd.startswith("$(", j):  # quotes inside a substitution do not end the string
                    j = _sub_end(cmd, j + 2)
                    continue
                j += 2 if cmd[j] == "\\" else 1
            if j >= n:
                out.append(cmd[i:])
                break
            out.append("''")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _segment_spans(cmd: str) -> list[tuple[int, int]]:
    """(start, end) of every simple command in `cmd`: split on `;`, `&&`, `||`, `|` and newlines outside quotes.
    Backslash escapes outside quotes are literal characters."""
    spans, start, quote, i, n = [], 0, None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote != "'" and cmd.startswith("$(", i):  # a substitution is one opaque word here
            i = _sub_end(cmd, i + 2)
            continue
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
        elif c == "\\" and i + 1 < n:
            i += 2
            continue
        elif c in "'\"":
            quote = c
        elif cmd.startswith(("&&", "||"), i):
            spans.append((start, i))
            i += 2
            start = i
            continue
        elif c in ";|\n":
            spans.append((start, i))
            start = i + 1
        i += 1
    spans.append((start, n))
    return [(a, b) for a, b in spans if cmd[a:b].strip()]


def _segments(cmd: str) -> list[str]:
    """Split a command line on `;`, `&&`, `||`, `|` and newlines that are outside quotes."""
    return [cmd[a:b].strip() for a, b in _segment_spans(cmd)]


@dataclass
class _Heredoc:
    delim: str
    strip_tabs: bool
    pos: int  # offset of the `<<` operator in the command text with bodies removed
    body: str = ""
    closed: bool = False
    # `<<'EOF'`, `<<"EOF"`, `<<\EOF`: the shell expands nothing in the body. With a bare `<<EOF` it still runs
    # `$( … )` and backticks in the body, so those stay subject to every check even when the body is data.
    quoted: bool = False


# `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`, `<<\WORD` (not the here-string `<<<`).
_HEREDOC_OP = re.compile(r"<<(-?)[ \t]*((?:'[^'\n]*'|\"[^\"\n]*\"|\\?[^\s;&|()<>'\"])+)")
_COMMENT_START = " \t\n;&|()"


def _read_body(cmd: str, i: int, doc: _Heredoc) -> int:
    """Consume `doc`'s body from offset `i` (the start of the line after the operator) up to and including its
    terminator line; returns the offset after it. Body lines are taken verbatim: no quotes, no splitting."""
    lines, n = [], len(cmd)
    while i < n:
        end = cmd.find("\n", i)
        end = n if end == -1 else end
        line = cmd[i:end]
        i = end + 1
        if (line.lstrip("\t") if doc.strip_tabs else line) == doc.delim:
            doc.body, doc.closed = "\n".join(lines), True
            return i
        lines.append(line)
    doc.body, doc.closed = "\n".join(lines), False  # unterminated: the rest of the command is its body
    return n


def _split_heredocs(cmd: str) -> tuple[str, list[_Heredoc]]:
    """(the command without heredoc bodies and comments, its heredocs in order). A shell-like scan: quotes and
    backslash escapes are tracked outside bodies only, so an apostrophe in a body ("don't") or in a comment cannot
    open a quote that hides the commands after it. Several heredocs on one line are read in order."""
    main: list[str] = []
    docs: list[_Heredoc] = []
    pending: list[_Heredoc] = []
    quote, i, n = None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if quote != "'" and cmd.startswith("$(", i):  # a substitution keeps its own quotes and heredocs
            end = _sub_end(cmd, i + 2)
            main.append(cmd[i:end])
            i = end
        elif quote:
            main.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                main.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
        elif c == "\\" and i + 1 < n:
            main.append(cmd[i:i + 2])
            i += 2
        elif c in "'\"":
            quote = c
            main.append(c)
            i += 1
        elif c == "#" and (i == 0 or cmd[i - 1] in _COMMENT_START):
            end = cmd.find("\n", i)
            i = n if end == -1 else end  # a comment: the shell ignores it up to the newline
        elif cmd.startswith("<<", i) and not cmd.startswith("<<<", i) and (m := _HEREDOC_OP.match(cmd, i)):
            delim = _strip_quotes_and_escapes(m.group(2))
            pending.append(_Heredoc(delim, bool(m.group(1)), len("".join(main)),
                                    quoted=any(q in m.group(2) for q in "'\"\\")))
            main.append(cmd[i:m.end()])
            i = m.end()
        elif c == "\n" and pending:
            main.append(c)
            i += 1
            for doc in pending:
                i = _read_body(cmd, i, doc)
                docs.append(doc)
            pending = []
        else:
            main.append(c)
            i += 1
    docs.extend(pending)  # an operator with no body line at all: unterminated, empty
    return "".join(main), docs


# Command words that never run code from stdin or from a file written in the same command. A heredoc body is data
# only when every simple command of the command line starts with one of these.
_DATA_ONLY_WORDS = frozenset({"cat", "tee", "gh", "git", "echo", "printf", "mkdir", "cd", "ls", "true", "pwd", "touch",
                              "wc", "head", "tail", "grep", "diff", "sort", "cp", "mv", "date", "test", "["})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_GH_BODY_STDIN = re.compile(r"(?:^|\s)(?:--body-file(?:=|\s+)|-F\s*)-(?=\s|$)")
_GIT_COMMIT_STDIN = re.compile(r"^git\s+(?:-[Cc]\s+\S+\s+)*commit\b.*\s(?:-F\s*|--file(?:=|\s+))-(?=\s|$)")


def _words(seg: str) -> list[str]:
    """The words of a simple command after leading `VAR=value` assignments and opening brackets."""
    words = _strip_quotes_and_escapes(seg).replace("(", " ").replace("{", " ").split()
    while words and _ASSIGNMENT.match(words[0]):
        words.pop(0)
    return words


def _all_data_only(main: str) -> bool:
    """Every simple command of `main` starts with a data-only word (see _DATA_ONLY_WORDS)."""
    return all((_words(main[a:b])[:1] or [""])[0] in _DATA_ONLY_WORDS for a, b in _segment_spans(main))


def _is_cat_heredoc(payload: str) -> bool:
    """`payload` (a `$( … )` body) is only `cat` reading one quoted heredoc: its output is the text, nothing runs."""
    main, docs = _split_heredocs(payload)
    if len(docs) != 1 or not docs[0].closed or not docs[0].quoted:
        return False
    segs = _segments(main)
    return len(segs) == 1 and [w for w in _words(segs[0]) if not w.startswith("<<")] == ["cat"]


def _is_data_heredoc(main: str, doc: _Heredoc) -> bool:
    """True when `doc`'s body is only text: its receiver is a text sink (`cat`, `tee`, `gh … --body-file -` /
    `-F -`, `git commit -F -`) and nothing in the command line runs code (every command word is data-only)."""
    if not doc.closed or not _all_data_only(main):
        return False
    spans = _segment_spans(main)
    receiver = next((main[a:b].strip() for a, b in spans if a <= doc.pos < b), "")
    first = (_words(receiver)[:1] or [""])[0]
    if first in ("cat", "tee"):
        return True
    plain = " ".join(_words(receiver))
    if first == "gh":
        return bool(_GH_BODY_STDIN.search(plain))
    if first == "git":
        return bool(_GIT_COMMIT_STDIN.search(plain))
    return False


_HERE_STRING = re.compile(r"<<<\s*(?:'([^']*)'|\"((?:\\.|[^\"\\])*)\"|(\S+))")


def _code_text(cmd: str) -> str:
    """`cmd` without its data heredoc bodies and comments (what the shell may run), for path checks on the
    command line: a heredoc that only writes prose does not touch the paths its prose mentions."""
    main, docs = _split_heredocs(cmd)
    parts = [main]
    if _all_data_only(main):
        for inner in _substitutions(main):
            if _is_cat_heredoc(inner):
                parts[0] = parts[0].replace(inner, "")
    for d in docs:
        if not _is_data_heredoc(main, d):
            parts.append(d.body)
        elif not d.quoted:
            parts.extend(_substitutions(d.body))
    return "\n".join(parts)


def _sub_end(cmd: str, j: int) -> int:
    """Index just past the `)` closing a `$(` whose payload starts at `j` (len(cmd) if unterminated).

    The payload is its own quoting context, as in the shell; nested `$(` are skipped recursively."""
    depth, quote, n = 1, None, len(cmd)
    pending: list[_Heredoc] = []
    while j < n:
        c = cmd[j]
        if quote is None and c == "\n" and pending:
            j += 1
            for doc in pending:  # heredoc bodies are verbatim: no quotes, no parentheses
                j = _read_body(cmd, j, doc)
            pending = []
            continue
        if quote is None and cmd.startswith("<<", j) and not cmd.startswith("<<<", j) and (
                m := _HEREDOC_OP.match(cmd, j)):
            pending.append(_Heredoc(_strip_quotes_and_escapes(m.group(2)), bool(m.group(1)), 0))
            j = m.end()
            continue
        if quote == "'":
            if c == "'":
                quote = None
        elif cmd.startswith("$(", j):
            j = _sub_end(cmd, j + 2)
            continue
        elif c == "\\":
            j += 1
        elif quote == '"':
            if c == '"':
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return n


def _substitutions(cmd: str) -> list[str]:
    """Payloads of every top-level `$( … )` and backtick substitution, wherever they appear.

    Inside double quotes the shell still runs them; inside single quotes it does not, but they are
    checked anyway (conservative). Nested substitutions are found when the payload is checked."""
    found, i, n = [], 0, len(cmd)
    while i < n:
        if cmd.startswith("$(", i):
            end = _sub_end(cmd, i + 2)
            found.append(cmd[i + 2:end - 1] if end <= n and cmd[end - 1:end] == ")" else cmd[i + 2:])
            i = end
        elif cmd[i] == "`":
            end = cmd.find("`", i + 1)
            end = n if end == -1 else end
            found.append(cmd[i + 1:end])
            i = end + 1
        else:
            i += 1
    return found


def _command_segments(cmd: str, depth: int = 0) -> list[str]:
    """Every simple command in `cmd`, including `$( … )`/backtick payloads (also inside quotes), the payloads of
    `sh -c "..."`, `eval "..."` and here-strings, and the body of every heredoc that is not plain data (see
    _is_data_heredoc). Inside a payload every heredoc counts as code: the outer command may run its output."""
    main, docs = _split_heredocs(cmd)
    result = []
    if depth < 5:
        data_only = _all_data_only(main)
        for inner in _substitutions(main):
            if data_only and _is_cat_heredoc(inner):
                continue  # `--body "$(cat <<'EOF' … EOF)"`: the output is an argument of a data-only command
            result.extend(_command_segments(inner, depth + 1))
    for seg in _segments(main):
        result.append(seg)
        if depth < 5:
            for m in _SHELL_PAYLOAD.finditer(seg):
                inner = m.group(1) if m.group(1) is not None else m.group(2)
                result.extend(_command_segments(inner, depth + 1))
            for m in _HERE_STRING.finditer(seg):
                inner = next(g for g in m.groups() if g is not None)
                result.extend(_command_segments(inner, depth + 1))
    for doc in docs:
        if depth >= 5:
            break
        if depth > 0 or not _is_data_heredoc(main, doc):
            result.extend(_command_segments(doc.body, depth + 1))
        elif not doc.quoted:
            for inner in _substitutions(doc.body):  # a bare `<<EOF` body still runs its substitutions
                result.extend(_command_segments(inner, depth + 1))
    return result


def _changes_hooks_path(seg: str, plain: str) -> bool:
    if not _GIT_WORD.search(plain):
        return False
    bare = seg.replace("'", "").replace('"', "")
    if _HOOKS_PATH_OVERRIDE.search(bare):
        return True
    return bool(_GIT_CONFIG.search(plain) and _HOOKS_PATH_KEY.search(bare) and not _CONFIG_READ.search(bare))


# `git config` (a write form) of a key that makes later git commands run a program or read other config
_GIT_EXEC_KEY = re.compile(r"(?i)\b(?:core\.(?:fsmonitor|sshcommand|pager|editor|askpass|gitproxy|alternaterefscommand|"
                           r"attributesfile|hookspath)|alias\.|include\.|includeif\.|filter\.|diff\.external|"
                           r"diff\.\S+\.(?:command|textconv)|difftool\.\S+\.cmd|mergetool\.\S+\.cmd|"
                           r"merge\.\S+\.driver|credential\.|sequence\.editor|gpg\.\S*program|"
                           r"gpg\.ssh\.defaultkeycommand|uploadpack\.packobjectshook|remote\.\S+\.(?:uploadpack|"
                           r"receivepack)|url\.\S*\.(?:insteadof|pushinsteadof)|protocol\.\S*allow)")
# A write into a repository's own files (redirect, tee, cp, mv, ln, install, rsync, sed -i, ...), or onto a bare
# `.git` target (`mv x .git`, `ln -s x .git`): see _GIT_DIR_DENIED. Matched after `//` and `/./` are collapsed.
_GIT_INTERNAL_PATH = re.compile(r"(?i)(?:^|[\s'\"=/~])\.git(?:/+(?:config|refs|packed-refs|info|objects|head|"
                                r"worktrees|modules|hooks|commondir|gitdir)\b|(?=$|[\s'\";&|)]))")
# The user's own git config: what every git of this user reads (a url rewrite, a filter, an attributes file)
_GLOBAL_GIT_CONFIG = re.compile(r"(?i)\.gitconfig\b|\.config/+git(?:/|\b)|XDG_CONFIG_HOME\}?/+git\b|/etc/gitconfig\b")
_GLOBAL_GIT_DENIED = ("agents do not write the user's own git config (~/.gitconfig, ~/.config/git, "
                      "$XDG_CONFIG_HOME/git, /etc/gitconfig) or point git config at it: every git of this user reads "
                      "it; ask the user")
# Claude Code's user-scope files decide which hooks guard every session (and the AI Factory runner tests the hook
# commands they name): its settings, its .claude.json and the plugins it installed. Agents never write them.
# Claude Code's user-scope files decide which hooks guard every session (and the AI Factory runner tests the hook
# commands they name): its settings, .claude.json, the plugins it installed, the user's hooks, skills, agents and
# CLAUDE.md, and the programs orch runs as (the folders of `orch` and `uv`, their tool venv, orch's own installed code).
# Agents never write them. In shell text: a write in a simple command whose text (quotes and backslashes taken out,
# $HOME spelled ~) names one, or any write after a `cd` into one.
_HARNESS_TEXT = re.compile(r"(?i)~/+\.claude(?:/|\.json\b|\b(?![\w.-]))|claude_config_dir")
_HARNESS_DENIED = ("agents do not write Claude Code's user-scope settings, its .claude.json, the plugins it installed, "
                   "the user's hooks, skills, agents or CLAUDE.md, or the programs orch runs as: they decide what guards "
                   "every session; ask the user")
_HARNESS_NAMES = ("settings.json", "settings.local.json", "CLAUDE.md")
_HARNESS_DIRS = ("plugins", "hooks", "skills", "agents")


def _program_folders(ws) -> set[Path]:
    """The folders of the `orch` and `uv` this process finds on its PATH (as found and after links), the tool venv a
    `bin` folder belongs to, and orch's own installed code; none inside the workspace (agents write there anyway) or
    in a source checkout (a folder with .git above it: someone develops orch there)."""
    import shutil
    out: set[Path] = set()
    for name in ("orch", "uv"):
        found = shutil.which(name)
        if found:
            real = Path(os.path.realpath(found)).parent
            out |= {Path(os.path.abspath(found)).parent, real}
            if real.name == "bin" and (real.parent / "pyvenv.cfg").is_file():
                out.add(real.parent)  # the tool venv that holds orch's code
    import orch as _orch
    out.add(Path(_orch.__file__).resolve().parent)
    root = Path(ws.root).resolve() if ws is not None else None

    def keep(p: Path) -> bool:
        if root is not None and (p == root or root in p.parents):
            return False
        return not any(os.path.lexists(d / ".git") for d in (p, *p.parents))
    return {p for p in out if keep(p)}


def _harness_targets(ws=None) -> tuple[set[str], set[str]]:
    """(files, folders), lower-cased absolute paths: the user-scope settings, CLAUDE.md and .claude.json (of
    CLAUDE_CONFIG_DIR and of ~/.claude), the plugins, hooks, skills and agents folders, every plugin folder
    installed_plugins.json names, and _program_folders. Errors leave out what cannot be read (the text check still
    applies)."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    homes = [Path.home(), Path.home().resolve()]
    configured = [Path(base), Path(base).resolve()] if base else []
    dirs = [*configured, *(h / ".claude" for h in homes)]  # the configured dir and the default one
    files = {str(d / n).lower() for d in dirs for n in _HARNESS_NAMES}
    files |= {str(d / ".claude.json").lower() for d in [*configured, *homes]}
    folders = {str(d / n).lower() for d in dirs for n in _HARNESS_DIRS}
    try:
        listed = json.loads((dirs[0] / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")).get("plugins")
        for entries in (listed.values() if isinstance(listed, dict) else []):
            for e in (entries if isinstance(entries, list) else [entries]):
                if isinstance(e, dict) and isinstance(e.get("installPath"), str) and os.path.isabs(e["installPath"]):
                    folders |= {e["installPath"].lower(), str(Path(e["installPath"]).resolve()).lower()}
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    try:
        folders |= {str(p).lower() for p in _program_folders(ws)}
    except (OSError, RuntimeError, ValueError, ImportError):
        pass
    return files, folders


def _harness_file(raw: str, cwd, ws) -> bool:
    """Whether a file tool's path is one of _harness_targets (as written or after symlinks). Any error is a yes."""
    if not raw:
        return False
    try:
        forms = _path_forms(raw, cwd, ws)
        files, folders = _harness_targets(ws)
    except (OSError, RuntimeError, ValueError):
        return True
    for f in forms:
        low = str(f).lower()
        if low in files or any(low == d or low.startswith(d + os.sep) for d in folders):
            return True
    return False


def _tilde(text: str) -> str:
    """Shell text with quotes and backslashes taken out, $HOME / ${HOME} and the home folder spelled ~, separators
    collapsed: so `"$HOME"/'.claude'/settings.json` reads ~/.claude/settings.json."""
    t = re.sub(r"[\\'\"]", "", text)
    t = re.sub(r"\$\{?HOME\}?", "~", t)
    for h in sorted({str(Path.home()), str(Path.home().resolve())}, key=len, reverse=True):
        t = t.replace(h, "~")
    return _paths_text(t)


def _harness_in_text(cmd: str, ws=None) -> bool:
    """Whether a shell command writes one of _harness_targets: a simple command that writes and whose text names one
    (only that command: `echo x > notes.md; cat ~/.claude/settings.json` is no hit), or any write after a `cd` or
    `pushd` into one in the same line. Any error is a yes."""
    try:
        files, folders = _harness_targets(ws)
        home = [str(Path.home()).lower(), str(Path.home().resolve()).lower()]

        def tilde(p: str) -> str:
            for h in home:
                if p.startswith(h):
                    return "~" + p[len(h):]
            return p
        marks = {tilde(x) for x in files | folders} | files | folders

        def names(text: str) -> bool:
            low = text.lower()
            return bool(_HARNESS_TEXT.search(text)) or any(m in low for m in marks)
        inside, here = False, None
        for seg in _command_segments(cmd):
            t = _tilde(seg)
            cd = re.match(r"^\s*(?:cd|pushd)(?:\s+(\S+))?", t)
            if cd:
                target = cd.group(1) or "~"
                here = target if target.startswith(("/", "~")) else (f"{here}/{target}" if here else None)
                inside = names(target + "/") or (here is not None and names(here + "/"))
                continue
            if not _is_git_write(seg):
                continue
            # a relative word after a cd counts from where that cd went (`cd ~ && echo x > .claude/settings.json`)
            rel = [f"{here}/{w}" for w in t.split() if here and not w.startswith(("/", "~", "-"))]
            if inside or names(t) or any(names(w) for w in rel):
                return True
        return False
    except Exception:
        return True


_RAW_TOKEN = re.compile(r"""(?:[^\s'"]+|'[^']*'|"(?:\\.|[^"\\])*")+""")


_LINK_WRITE = re.compile(r"(?:^|[\s;&|(`'\"])(?:ln|install|rsync)\b")


def _is_git_write(cmd: str) -> bool:
    """_is_write, plus ln, install and rsync: for the .git and git-config checks only (elsewhere a hard link out of
    a guarded file stays as it was ruled)."""
    return _is_write(cmd) or bool(_LINK_WRITE.search(cmd))


def _paths_text(text: str) -> str:
    """`text` with backslash separators, repeated separators and `/./` collapsed, for the path checks above."""
    t = re.sub(r"[/\\]+", "/", text)
    while "/./" in t:
        t = t.replace("/./", "/")
    return t


def _word(tok: str) -> str | None:
    """The literal value of one shell word, or None when it is not a plain literal: it holds `$`, a backtick or a
    backslash, or a quote inside it (`g'i't`), so its value is only known when the shell runs it."""
    import shlex
    if re.search(r"[$`\\]", tok):
        return None
    if ("'" in tok or '"' in tok) and not re.fullmatch(r"'[^']*'|\"[^\"]*\"|[^'\"]+", tok):
        return None
    try:
        w = shlex.split(tok)
    except ValueError:
        return None
    return w[0] if len(w) == 1 else None


def _sets_git_exec_config(seg: str, plain: str) -> bool:
    """A `git config` write, a `git -c`, or `git --config-env` that sets a key which runs a program or redirects git
    (`_GIT_EXEC_KEY`), writes the user's global or system config, or points `--file` into `.git` or the user's
    config; and any such command whose `git` word, key or file is not a plain literal (`$'..'`, `${..}`, `$(..)`,
    backslashes, quotes inside a word): its value is unknown here, so it is denied (fail closed)."""
    if _GIT_WORD.search(plain) and _GIT_CONFIG.search(plain):  # the plain spelling, as before
        bare = seg.replace("'", "").replace('"', "")
        if _GIT_EXEC_KEY.search(bare) and not _CONFIG_READ.search(bare):
            return True
    toks = _RAW_TOKEN.findall(seg)
    vals = [_word(t) for t in toks]
    first = next((k for k, t in enumerate(toks) if not re.match(r"[A-Za-z_]\w*=", t)), len(toks))
    for i, v in enumerate(vals):
        # a command word whose value is unknown here (`$'\x67it'`, `${G}`, `g\it`, `g'i't`) may be git
        hidden = i == first and v is None
        if not (hidden or (v is not None and os.path.basename(v).lower() in ("git", "git.exe"))):
            continue
        rest, rv = toks[i + 1:], vals[i + 1:]
        lits = [x or "" for x in rv]
        # the options before git's subcommand (the first plain word that is not an option or an option's value)
        takes = ("-C", "-c", "--git-dir", "--work-tree", "--namespace")
        sub = next((j for j, x in enumerate(rv) if x is not None and not x.startswith("-")
                    and not (j and lits[j - 1] in takes)), len(rv))
        ci = sub if sub < len(rv) and lits[sub] == "config" else None
        head = range(sub)
        if any(lits[j].startswith("--config-env") for j in head):
            return True
        # -c, and any word in the options' place whose value is unknown (it may be -c, config or --config-env),
        # except the value of an option that takes a path
        cs = [j for j in head if lits[j] == "-c" or (lits[j].startswith("-c") and len(lits[j]) > 2)
              or (rv[j] is None and not (j and lits[j - 1] in ("-C", "--git-dir", "--work-tree", "--namespace"))
                  and (not hidden or rest[j].startswith("$'")))]
        if ci is None and not cs:
            continue
        if hidden:
            return True  # a git word whose value is unknown, with config, -c or an unknown word after it
        for j in cs:
            kv = rv[j + 1] if lits[j] == "-c" and j + 1 < len(rv) else (rv[j][2:] if rv[j] else None)
            if kv is None or rv[j] is None or _GIT_EXEC_KEY.search(kv.split("=", 1)[0] + "="):
                return True
        if ci is None:
            continue
        args, key, skip = rv[ci + 1:], None, False
        for k, a in enumerate(args):  # options and the key must be plain literals; the value may be anything
            if skip:
                skip = False
                continue
            if a is None:
                return True
            if a in ("-f", "--file", "--type", "--blob", "--default", "--comment"):
                skip = a in ("--type", "--default", "--comment")
                if a in ("-f", "--file"):
                    target = args[k + 1] if k + 1 < len(args) else None
                    if target is None:
                        return True
                continue
            if not a.startswith("-"):
                key = a
                break
        joined = " " + " ".join(["config", *[a for a in args if a is not None]])
        if _CONFIG_READ.search(joined):
            continue
        for k, a in enumerate(args):
            if a in ("--global", "--system"):
                return True
            target = args[k + 1] if a in ("-f", "--file") and k + 1 < len(args) else (
                a.split("=", 1)[1] if isinstance(a, str) and a.startswith("--file=") else None)
            if target is not None:
                t = _paths_text(target)
                if _GLOBAL_GIT_CONFIG.search(t) or any(part.lower() == ".git" for part in t.split("/")):
                    return True
        if key is not None and _GIT_EXEC_KEY.search(key + "="):
            return True
    return False


def _cd_targets_state(cmd: str) -> bool:
    for m in _CD.finditer(cmd):
        target = next(g for g in m.groups() if g is not None)
        parts = re.split(r"[/\\]", target)
        if "tickets" in parts or ".state" in parts:
            return True
    return False


def _config_ancestor_forms() -> set[str]:
    """Symbolic and resolved spellings of the orch config dir and the directory right above it (normally
    `~/.config`, however `XDG_CONFIG_HOME` is set), and the home directory in every spelling (`~`, `$HOME`,
    the literal path and its resolved form): a recursive read or archive rooted at any of these can
    still walk down into remote-humans.json. Bounded to one level up, not every ancestor to the filesystem
    root, so this stays a cheap, best-effort check rather than a blanket deny on broad reads."""
    from orch.dashboard.launch import config_dir
    forms = {"~", "$HOME", "${HOME}", "~/.config", "$HOME/.config", "${HOME}/.config",
             "$XDG_CONFIG_HOME", "${XDG_CONFIG_HOME}"}
    for base in {config_dir(), *_user_config_dirs()}:
        forms.add(str(base))
        forms.add(str(base.parent))
    home = Path.home()
    forms.add(str(home))  # the literal home path, as `~` expands to it
    try:
        forms.add(str(home.resolve()))
    except (OSError, RuntimeError):
        pass
    return forms


def _higher_ancestors() -> set[str]:
    """Every directory above the home dir and the config dir's parent, except `/` itself (`/Users`,
    `/home`, ...): a recursive read rooted there also reaches the pairing keys."""
    from orch.dashboard.launch import config_dir
    out: set[str] = set()
    for base in {config_dir(), *_user_config_dirs(), Path.home()}:
        for p in base.parents:
            if str(p) != p.anchor:
                out.add(str(p))
    return out


def _config_dir_res(cmd: str) -> list[re.Pattern]:
    from orch.dashboard.launch import config_dir
    literal = ({str(d) for d in _user_config_dirs()} | {str(config_dir())} | _config_ancestor_forms()
               | _higher_ancestors())
    forms = "|".join([_CONFIG_DIR_FORMS] + [re.escape(x) for x in sorted(literal, key=len, reverse=True)])
    return [re.compile(rf"(?:{forms})[/\\]?[^\s'\"/\\]*[*?\[]"),            # a glob right inside the dir
            # the dir (or an ancestor, the filesystem root included) as an operand
            re.compile(rf"(?:(?:{forms})[/\\]?|(?:^|(?<=[\s'\"=]))/)(?=$|[\s'\";|&)`])")]


_BACKSLASH = re.compile(r"\\(.)")
_QUOTE_CHAR = re.compile(r"['\"]")
_SIMPLE_BRACE = re.compile(r"\{([^{}]{1,200})\}")
_ASSIGN = re.compile(r'(?:^|[;&\n]|&&|\|\|)\s*(?:(?:export|declare|typeset|local|readonly)(?:\s+-\w+)*\s+)?'
                     r'([A-Za-z_][A-Za-z0-9_]*)=("[^"]*"|\'[^\']*\'|[^\s;&|]*)')
# `$(echo X)` and `` `echo X` ``: the shell prints X back, so read X in their place.
_ECHO_SUB = re.compile(r"\$\(\s*echo\s+([^()`]*?)\s*\)|`\s*echo\s+([^`]*?)\s*`")
# A word in a command line that may be a path (no option, no assignment), for the path-token check.
_PATH_TOKEN = re.compile(r"(?<![^\s;&|()<>])([^\s;&|()<>`'\"=-][^\s;&|()<>`'\"]*)")
# `ln -s`, `-sf`, `-sfn`, `-nfs`, `--symbolic`, in any position after `ln`.
_SYMLINK = re.compile(r"\bln\b[^;&|\n]*\s(?:-\w*s\w*|--symbolic)\b")


def _strip_quotes_and_escapes(s: str) -> str:
    """What the shell would join into one word: drop quote characters (keep their content) and backslash
    escapes, so `o''rch`, `"orch"` and `o\\rch` all read as `orch`. Best effort, not a real shell parser;
    biased to over-match (a false deny costs nothing, a bypass costs the pairing keys)."""
    return _QUOTE_CHAR.sub("", _BACKSLASH.sub(r"\1", s))


def _expand_braces(s: str, budget: int = 32) -> list[str]:
    """Every expansion of the first few simple, non-nested `{a,b,c}` groups (`re{mote-hu,}mans.json` ->
    `remote-humans.json`, `remans.json`), capped so a pathological command cannot blow up the check."""
    m = _SIMPLE_BRACE.search(s)
    if not m or budget <= 1:
        return [s]
    pre, post, opts = s[:m.start()], s[m.end():], m.group(1).split(",")
    out: list[str] = []
    for opt in opts:
        if len(out) >= budget:
            break
        out.extend(_expand_braces(pre + opt + post, budget - len(out)))
    return out or [s]


def _resolve_vars(cmd: str) -> str:
    """`$VAR`/`${VAR}` replaced by the value of a `VAR=value` assignment earlier in the same command
    (`D=~/.config/orch; cat "$D/x"`), so later detection sees the real path. Best effort: the first
    assignment of a name wins, and a name reassigned later in the same command keeps its first value."""
    assigns: dict[str, str] = {}
    for m in _ASSIGN.finditer(cmd):
        name, val = m.group(1), m.group(2)
        if name not in assigns:
            assigns[name] = val[1:-1] if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'" else val
    out = cmd
    for name, val in sorted(assigns.items(), key=lambda kv: len(kv[0]), reverse=True):
        out = out.replace("${" + name + "}", val).replace("$" + name, val)
    return out


def _key_check_candidates(cmd: str) -> list[str]:
    """`cmd`, plus a best-effort normalisation (variables resolved, quotes/escapes dropped, simple braces
    expanded) for the pairing-key check only: every candidate is checked, and any one matching is enough."""
    resolved = _resolve_vars(cmd)
    resolved = _ECHO_SUB.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), resolved)
    normalised = _strip_quotes_and_escapes(resolved)
    return [cmd] + _expand_braces(normalised)


def _config_paths() -> tuple[set[str], set[str]]:
    """(the orch config dir, the config dir plus the dirs whose recursive read reaches it), as plain absolute
    path strings in raw and resolved spelling: the dir, its parent (normally ~/.config) and the home dir."""
    from orch.dashboard.launch import config_dir
    dirs: set[str] = set()
    for d in {config_dir(), *_user_config_dirs()}:
        dirs.add(str(d))
    ancestors = set(dirs)
    for d in {config_dir(), *_user_config_dirs()}:
        ancestors.add(str(d.parent))
    home = Path.home()
    ancestors.add(str(home))
    try:
        ancestors.add(str(home.resolve()))
    except (OSError, RuntimeError):
        pass
    ancestors |= _higher_ancestors()
    ancestors.add("/")
    return dirs, ancestors


def _expand_path_token(tok: str, cwd) -> str | None:
    """`tok` as a normalised absolute path: `~`, `$HOME` and `$XDG_CONFIG_HOME` expanded, a relative path joined
    to `cwd` (when the payload has one), `./`, `//` and `..` collapsed. None when it is not a path we can place."""
    import os

    home = str(Path.home())
    xdg = os.environ.get("XDG_CONFIG_HOME") or home + "/.config"
    for pre, val in (("${XDG_CONFIG_HOME}", xdg), ("$XDG_CONFIG_HOME", xdg), ("${HOME}", home), ("$HOME", home),
                     ("~", home)):
        if tok == pre or tok.startswith(pre + "/"):
            tok = val + tok[len(pre):]
            break
    if "$" in tok:
        return None
    if not tok.startswith("/"):
        if not cwd or not str(cwd).startswith("/"):
            return None
        tok = os.path.join(str(cwd), tok)
    return os.path.normpath(tok)


def _path_hits(path: str, targets: set[str]) -> bool:
    """`path` (possibly a glob) is one of `targets`, in its own or its resolved spelling."""
    import fnmatch
    import os

    if path in targets or (_GLOB.search(path) and any(fnmatch.fnmatch(t, path) for t in targets)):
        return True
    try:
        return os.path.realpath(path) in targets
    except (OSError, ValueError):
        return False


def _path_tokens_reach(cmd: str, cwd) -> bool:
    """Every path-like word placed as an absolute, normalised path (see _expand_path_token): a glob right inside
    the config dir or a dir above it, or such a dir as the operand of a recursive reader or `ln -s`, or as a `cd`
    target followed by a glob or a recursive reader. Catches `~/.config/./orch/*`, `~/.config/orch/..`, and
    `tar czf x.tgz orch` or `cat *` run from inside ~/.config."""
    import os

    dirs, ancestors = _config_paths()
    reader = bool(_DIR_READER.search(cmd) or _SYMLINK.search(cmd))
    for n, m in enumerate(_PATH_TOKEN.finditer(cmd)):
        if n > 200:
            break
        path = _expand_path_token(m.group(1), cwd)
        if path is None:
            continue
        if reader and _path_hits(path, ancestors):
            return True
        if _GLOB.search(os.path.basename(path)) and _path_hits(os.path.dirname(path), ancestors):
            return True
    for m in _CD.finditer(cmd):
        target = next(g for g in m.groups() if g is not None)
        path = _expand_path_token(target, cwd)
        if path is not None and _path_hits(path, ancestors):
            rest = _unquoted(cmd[m.end():])
            if _GLOB.search(rest) or _DIR_READER.search(rest):
                return True
    return False


def _reaches_pairing_keys_1(cmd: str) -> bool:
    if _REMOTE_KEYS.search(cmd):
        return True
    if _INTERPRETER.search(cmd) and _REMOTE_PY.search(cmd):
        return True
    glob_in_dir, dir_itself = _config_dir_res(cmd)
    if glob_in_dir.search(cmd) or (dir_itself.search(cmd) and (_DIR_READER.search(cmd) or _SYMLINK.search(cmd))):
        return True
    for m in _CD.finditer(cmd):  # `cd ~/.config/orch && cat *` or `... && grep -r key .`
        target = next(g for g in m.groups() if g is not None)
        if dir_itself.search(target + " "):
            rest = _unquoted(cmd[m.end():])
            if _GLOB.search(rest) or _DIR_READER.search(rest):
                return True
    return False


def _reaches_pairing_keys(cmd: str, cwd=None) -> bool:
    """Best-effort, not a sandbox (spec §10): an agent with a real shell can still get around string
    matching. This raises the bar; it does not claim to be unbeatable.

    What it normalises: quotes and backslash escapes, simple `{a,b}` braces, `VAR=`/`export`/`declare`
    assignments made earlier in the same command, `$(echo X)`, `~`/`$HOME`/`$XDG_CONFIG_HOME`, `./`, `//`
    and `..`, relative paths against the payload `cwd`, `cd`/`cd --`/`cd -P`/`pushd` targets.

    Known limits, left open on purpose (a shell agent can always get here some way):
    - interpreters and scripts: `python`/`perl`/`node`/`ruby` building the path at run time, or a script
      written to disk first and run next;
    - ANSI-C quoting (`$'\x72emote'`), `printf`/`tr`/`base64` decoding a name, here-strings and heredocs;
    - variables set from command output (`D=$(dirname ...)`), in an earlier command, or reassigned later;
    - nested or numeric braces (`{a,{b,c}}`, `{1..3}`), aliases and shell functions;
    - `cd` changing what a later relative path means, beyond the one `cd` followed by a glob or a
      recursive reader;
    - tools this does not know as recursive readers, and symlinks or hard links made by other means."""
    return any(_reaches_pairing_keys_1(c) or _path_tokens_reach(c, cwd) for c in _key_check_candidates(cmd))


def _pairing_key_path(raw: str, *, dirs_too: bool = False) -> bool:
    """`raw` (a file tool's path) names remote-humans.json, resolves to it (also through a symlink), or, for Grep,
    is the orch config dir that holds it."""
    if not raw:
        return False
    if _REMOTE_KEYS.search(raw):
        return True
    try:
        p = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    bases = _user_config_dirs()
    return any(p == b / "remote-humans.json" for b in bases) or (dirs_too and p in bases)


def _resolve_root(raw) -> Path | None:
    text = str(raw or "")
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _is_config_dir_or_ancestor(root: Path) -> bool:
    """True when `root` is the orch config dir, or a directory above it (`~`, `~/.config`, `/`, the default cwd
    when it happens to be one of those, ...): a recursive tool rooted there can walk down into it."""
    return any(_under(b, root) for b in _user_config_dirs())


# ripgrep file types (`rg --type-list`) none of whose globs can match a `.json` file such as remote-humans.json.
# An allowlist: any other type (`json`, `jsonl`, `config`, `all`, a custom one, ...) is assumed to reach it.
_SAFE_RG_TYPES = frozenset({"py", "md", "markdown", "txt", "rust", "go", "java", "js", "ts", "html", "css", "yaml",
                            "toml", "sh", "c", "cpp", "sql", "csv", "xml", "kotlin", "swift", "lua", "php", "scala"})


def _glob_could_reach_key(pattern) -> bool:
    """True unless a Grep/Glob glob plainly cannot match `remote-humans.json`. Only a plain glob that fnmatch
    rejects for that name is safe: no glob, `**` or a directory part, a `{a,b}` alternation (fnmatch does not
    expand it) and a `!` negation (it selects everything else) are all assumed to reach it."""
    import fnmatch

    text = str(pattern or "")
    if not text or "**" in text or "{" in text or text.startswith("!"):
        return True
    name = text.rsplit("/", 1)[-1]
    return fnmatch.fnmatch("remote-humans.json", name)


def _filter_could_reach_key(glob=None, type_=None) -> bool:
    """True unless a Grep/Glob filter plainly rules out `remote-humans.json`: no filter at all reaches it, and
    every filter given must be safe on its own (a ripgrep glob override can re-include what a type excludes)."""
    if not glob and not type_:
        return True
    if glob and _glob_could_reach_key(glob):
        return True
    return bool(type_) and str(type_) not in _SAFE_RG_TYPES


def _reaches_config_dir_recursively(raw_root, cwd, glob=None, type_=None) -> bool:
    """A Read/Grep/Glob rooted at, or above, the orch config dir (explicitly, or by falling back to `cwd`),
    whose own filter does not plainly rule out `remote-humans.json`."""
    root = _resolve_root(raw_root) or _resolve_root(cwd)
    return bool(root) and _is_config_dir_or_ancestor(root) and _filter_could_reach_key(glob, type_)


def _ledger_path(raw: str, cwd=None) -> bool:
    """`raw` (a file tool's path or glob) names the ledger or its key, or resolves into the ledger dir. Relative paths
    are taken from the hook's cwd, resolved once, and a path with a symlink component that leads into the config dir
    is refused as written. A path that cannot be resolved is refused."""
    if not raw:
        return False
    if _LEDGER.search(raw):
        return True
    from orch.core import ledger
    try:
        if len(raw) > 4096 or raw.count("/") > MAX_PARTS:
            return True
        text = os.path.expanduser(raw)
        try:
            base = ledger.base_dir().resolve()
        except (OSError, RuntimeError):
            base = ledger.base_dir()
        for start in {str(cwd) if cwd else os.getcwd(), os.getcwd()}:  # the hook's cwd and this process's own
            full = text if os.path.isabs(text) else os.path.join(start, text)
            p = _real(full)
            if _link_into(full, base, _Budget()) or p in (
                    base / ledger.KEY_NAME, base / ledger.LEDGER_FILE, base / ledger.HEAD_FILE, base / ledger.LOCK_FILE
            ) or p == base / "permits" or (base / "permits") in p.parents:
                return True
    except (OSError, RuntimeError, ValueError, _Bound):
        return True
    return p in (base / ledger.KEY_NAME, base / ledger.LEDGER_FILE, base / ledger.HEAD_FILE, base / ledger.LOCK_FILE) or p == base / "permits" or (base / "permits") in p.parents


def _ledger_path_closed(raw: str, cwd=None) -> bool:
    """_ledger_path, where an error in its resolution rules (symlinks, bounds) is a deny. Only this new part fails
    closed; an unrelated error elsewhere in the guard still fails open as it always did."""
    try:
        return _ledger_path(raw, cwd)
    except Exception:
        return True


def _filter_could_reach_ledger(pattern: str) -> bool:
    """True unless a Grep/Glob filter plainly cannot match the ledger files (`ledger.jsonl`, `ledger.key`)."""
    import fnmatch
    if (not pattern or "**" in pattern or "{" in pattern or pattern.startswith("!") or "ledger" in pattern.lower()
            or "permits" in pattern.lower()):
        return True
    name = pattern.rsplit("/", 1)[-1]
    # the ledger files and the shapes of the permit records (a request body, a once-use marker)
    return any(fnmatch.fnmatch(n, name) for n in ("ledger.key", "ledger.jsonl", "ledger.head", "ledger.lock", "P-0123ABCD.json", "0123456789abcdef"))


def _bash_reaches_ledger(cmd: str) -> bool:
    from orch.core import ledger
    if any(_LEDGER.search(c) for c in _key_check_candidates(cmd)):
        return True
    base = str(ledger.base_dir())
    return any(f"{base}{sep}{name}" in cmd for sep in ("/", "\\") for name in (ledger.KEY_NAME, ledger.LEDGER_FILE, ledger.HEAD_FILE, ledger.LOCK_FILE, "permits"))


def _factory_commit(ws, payload: dict, command=None) -> Decision | None:
    """An AI Factory session the runner bound runs only the allowlisted git commands, and commits only in its own work
    tree (orch.core.permits.commit_refusal, the one function the permission hook calls too).
    Checked here, in every permission mode (an allow rule, auto mode or bypass never reach the PermissionRequest hook,
    which checks it again). A session whose binding exists but does not verify is refused; any other session is left
    alone."""
    try:
        from orch.core import factory_sessions, permits
        state, b = factory_sessions.session_state(ws, payload.get("session_id"))
        if state == "none":
            return None
        why = (permits.bash_gate(ws, b, payload) if state == "trusted"
               else "orch cannot tell whether this is an AI Factory session (its binding does not verify)")
    except Exception as e:
        why = f"orch could not check where an AI Factory session would commit ({type(e).__name__})"
    if why:
        return Decision(False, f"refused in this AI Factory session: {why}. Leave your changes in the working tree and say so "
                               "with orch log; do not retry it in another form.")
    return None


def evaluate(ws, payload: dict) -> Decision:
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd")
    if tool == "Bash":  # a bound session's shell call, whatever its input, goes through the shared gate first
        commit = _factory_commit(ws, payload)
        if commit is not None:
            return commit
    if not isinstance(tool_input, dict):
        return ALLOW
    command = tool_input.get("command")
    if tool == "Bash" and isinstance(command, str) and "\\\n" in command:
        # A backslash-newline continues the line in the shell: judge the joined text as well (it only adds denials).
        joined = evaluate(ws, {**payload, "tool_input": {**tool_input, "command": command.replace("\\\n", "")}})
        if not joined.allow:
            return joined
    if tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "Glob") and any(
            _ledger_path_closed(str(tool_input.get(k) or ""), cwd) for k in ("file_path", "notebook_path", "path", "pattern",
                                                                    "glob")):
        return Decision(False, _LEDGER_DENIED)
    if tool == "Bash" and _bash_reaches_ledger(str(tool_input.get("command") or "")):
        return Decision(False, _LEDGER_DENIED)
    if tool in ("Grep", "Glob"):
        root = _resolve_root(tool_input.get("path")) or _resolve_root(cwd)
        filt = str(tool_input.get("glob") or (tool_input.get("pattern") if tool == "Glob" else "") or "")
        type_ = str(tool_input.get("type") or "") if tool == "Grep" else ""
        safe_type = bool(type_) and type_ in _SAFE_RG_TYPES and not tool_input.get("glob")
        if (root is not None and _is_config_dir_or_ancestor(root) and not safe_type
                and _filter_could_reach_ledger(filt)):
            return Decision(False, _LEDGER_DENIED)
    if tool in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit") and _pairing_key_path(
            str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")):
        return Decision(False, _REMOTE_DENIED)
    if tool == "Grep" and (_pairing_key_path(str(tool_input.get("path") or ""), dirs_too=True)
                           or _REMOTE_KEYS.search(str(tool_input.get("glob") or ""))):
        return Decision(False, _REMOTE_DENIED)
    if tool == "Grep" and _reaches_config_dir_recursively(tool_input.get("path"), cwd, tool_input.get("glob"),
                                                          tool_input.get("type")):
        return Decision(False, _CONFIG_SECRETS_DENIED)
    if tool == "Glob" and _REMOTE_KEYS.search(str(tool_input.get("pattern") or "")):
        return Decision(False, _REMOTE_DENIED)
    if tool == "Glob" and _reaches_config_dir_recursively(tool_input.get("path"), cwd, tool_input.get("pattern")):
        return Decision(False, _CONFIG_SECRETS_DENIED)
    if tool == "Read" and _reaches_config_dir_recursively(tool_input.get("file_path"), None):
        return Decision(False, _CONFIG_SECRETS_DENIED)  # Read given a directory: some clients list it
    if tool == "Bash":
        return _bash(ws, str(tool_input.get("command") or ""), cwd)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit") and _in_git_dir(
            str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), cwd, ws):
        hooks = _STARTUP_FILE.search(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""))
        return Decision(False, _STARTUP_DENIED if hooks else _GIT_DIR_DENIED)  # git hooks keep their own message
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit") and _global_git_config(
            str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), cwd, ws):
        return Decision(False, _GLOBAL_GIT_DENIED)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit") and _harness_file(
            str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), cwd, ws):
        return Decision(False, _HARNESS_DENIED)
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        why = _clone_file(str(tool_input.get("file_path") or tool_input.get("notebook_path") or ""), cwd, ws)
        if why:
            return Decision(False, why)
    if tool in ("Edit", "Write", "MultiEdit"):
        return _edit(ws, tool, tool_input)
    return ALLOW


_CLONE_TICKETS_DENIED = ("this is a child clone's copy of the orch folder (tickets, state, config): tickets change "
                         "only through orch, which works on the workspace's own tickets from here too")
_CLONE_HARNESS_DENIED = ("a child clone's harness settings (.claude/settings.json, .claude/settings.local.json, "
                         ".mcp.json) are not written: the runner refuses to start a session in a clone whose settings "
                         "differ from the workspace's")


def _clone_file(raw: str, cwd, ws) -> str | None:
    """Why a file tool may not write `raw` inside one of the runner's child clones, or None: the clone's copy of the
    workspace's orch folder (tickets, .state, config.json) and its harness settings, matched without case, as written
    or after links."""
    if not raw:
        return None
    try:
        from orch.core import factory_clones
        from orch.core.factory_release import always_sensitive
        forms = _path_forms(raw, cwd, ws)
        roots = {Path(os.path.normpath(factory_clones.root())), factory_clones.root().resolve()}
        home = always_sensitive(ws)[0].strip("/").casefold()
    except Exception:
        return None
    for f in forms:
        for r in roots:
            try:
                rest = f.relative_to(r).parts[3:]  # <workspace id>/<child>/repo/...
            except ValueError:
                continue
            text = "/".join(rest).casefold()
            if (text in (f"{home}/config.json", f"{home}/tickets", f"{home}/.state")
                    or text.startswith((f"{home}/tickets/", f"{home}/.state/"))):
                return _CLONE_TICKETS_DENIED
            if re.search(r"(?:^|/)\.claude/settings(?:\.local)?\.json$|(?:^|/)\.mcp\.json$", text):
                return _CLONE_HARNESS_DENIED
    return None


# A repository's own files (`.git/config`, refs, packed-refs, info, objects/info/alternates, HEAD, worktrees, hooks,
# or a worktree's `.git` pointer file) decide what later git commands run and see: hooksPath, fsmonitor, aliases,
# refs. Agents write none of them with their file tools, in any workspace or worktree; reading stays open.
_GIT_DIR_DENIED = ("agents do not write inside .git or a .git file with their file tools (config, refs, packed-refs, "
                   "info, objects, HEAD, worktrees, hooks): later git commands would run or trust what is written "
                   "there; use git commands that the guard allows instead")


def _path_forms(raw: str, cwd, ws) -> set[Path]:
    """A file tool's path as written (`~` expanded, relative to the hook's working directory, else the workspace
    root) and after symlinks; raises on a path that cannot be worked out."""
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path(str(cwd)) / p if cwd else Path(ws.root) / p
    return {Path(os.path.normpath(p)), p.resolve()}


def _global_git_config(raw: str, cwd, ws) -> bool:
    """Whether a file tool's path is the user's own git config (any case, as written or after symlinks):
    ~/.gitconfig, anything in ~/.config/git or $XDG_CONFIG_HOME/git, /etc/gitconfig. Any error is a yes."""
    if not raw:
        return False
    try:
        forms = _path_forms(raw, cwd, ws)
        home = [Path.home(), Path.home().resolve()]
        bases = [h / ".config" / "git" for h in home]
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            bases += [Path(xdg) / "git", (Path(xdg) / "git").resolve()]
        files = {str(h / ".gitconfig").lower() for h in home} | {"/etc/gitconfig", "/private/etc/gitconfig"}
        dirs = {str(b).lower() for b in bases}
    except (OSError, RuntimeError, ValueError):
        return True
    for f in forms:
        low = str(f).lower()
        if low in files or any(low == d or low.startswith(d + os.sep) for d in dirs):
            return True
    return False


def _in_git_dir(raw: str, cwd, ws) -> bool:
    """Whether a file tool's path has a `.git` component (any case), as written (relative to the hook's working
    directory, else the workspace root) or after symlinks are resolved. Any error is a yes."""
    if not raw:
        return False
    try:
        forms = _path_forms(raw, cwd, ws)
    except (OSError, RuntimeError, ValueError):
        return True
    return any(part.lower() == ".git" for f in forms for part in f.parts)


def _cwd_in_state(ws, cwd) -> bool:
    if not cwd:
        return False
    try:
        p = Path(str(cwd))
        p = (p if p.is_absolute() else ws.root / p).resolve()
    except (OSError, ValueError):
        return False
    return _under(p, ws.tickets_dir.resolve()) or _under(p, ws.state_dir.resolve())


def _ticket_file_re(ws) -> re.Pattern:
    prefix = re.escape(str(ws.config["id"]["prefix"]))
    return re.compile(rf"\b{prefix}-\d+[-\w.]*\.md\b")


def _touches_state(ws, cmd: str, cwd) -> bool:
    return bool(
        _STATE.search(cmd)
        or _cwd_in_state(ws, cwd)
        or _cd_targets_state(cmd)
        or _ticket_file_re(ws).search(cmd)
    )


def _user_config_dirs() -> list[Path]:
    """The orch config dir as `launch.config_dir()` resolves it, plus the default ~/.config/orch (the guard may run
    with a different environment than `orch serve`)."""
    from orch.dashboard.launch import config_dir
    out = []
    for d in (config_dir(), Path.home() / ".config" / "orch"):
        try:
            d = d.resolve()
        except (OSError, RuntimeError):
            pass
        if d not in out:
            out.append(d)
    return out


def _user_addon_path(text: str) -> bool:
    for base in _user_config_dirs():
        if any(f"{base}{sep}{name}" in text for sep in ("/", "\\") for name in ("addons.json", "workspaces.json", "addons")):
            return True
    from orch.dashboard.launch import config_dir
    raw = str(config_dir())  # unresolved spelling too (/var vs /private/var on macOS)
    if any(f"{raw}{sep}{name}" in text for sep in ("/", "\\") for name in ("addons.json", "workspaces.json", "addons")):
        return True
    return bool(_USER_ADDON_FORMS.search(text))


def _config_dir_indirect(cmd: str) -> bool:
    """A config-dir spelling (or the resolved dir) and an addon file name in the same command."""
    if not _ADDON_FILE_NAME.search(cmd):
        return False
    if _CONFIG_BASE.search(cmd):
        return True
    from orch.dashboard.launch import config_dir
    return any(str(base) in cmd for base in _user_config_dirs() + [config_dir()])


def _is_user_addon_file(path: Path) -> bool:
    return any(path in (base / "addons.json", base / "workspaces.json") or _under(path, base / "addons")
               for base in _user_config_dirs())


def _is_write(cmd: str) -> bool:
    return bool(_WRITE_TOOL.search(cmd) or _OTHER_WRITE.search(cmd)
                or _OUTPUT_REDIRECT.search(_unquoted(cmd)) or _INTERP_WRITE.search(cmd))


def _bash(ws, cmd: str, cwd=None) -> Decision:
    if _reaches_pairing_keys(cmd, cwd):
        named = any(_REMOTE_KEYS.search(c) for c in _key_check_candidates(cmd))
        return Decision(False, _REMOTE_DENIED if named else _CONFIG_SECRETS_DENIED)
    try:
        touches = _touches_state_dir(ws, cmd, cwd)
        risky = touches or _mux_risky(cmd)
    except Exception:  # a limit, or anything unexpected: never an allow
        return Decision(False, _BOUND_DENIED)
    if touches == "unknown":
        return Decision(False, _UNKNOWN_DENIED)
    if touches:
        return Decision(False, _STATE_DENIED)
    if risky:
        named = _ORCH_TMUX.search(cmd.replace("'", "").replace('"', ""))
        return Decision(False, _ORCH_TMUX_DENIED if named else _MUX_DENIED)
    may = ws.config["git"]["agent_may"]
    term = ws.config["git"]["review_term"]
    for seg in _command_segments(cmd):
        # git checks look at the command with quoted text blanked out, so a commit message or an
        # echo that mentions `git push` or `-n` is not mistaken for the command itself.
        plain = _unquoted(seg)
        if _SERVE.search(plain) or _QUOTED_SERVE.search(seg):
            return Decision(False, _SERVE_DENIED)
        if _ORCH_TMUX.search(seg.replace("'", "").replace('"', "")):  # quotes removed: -L "orch" is -L orch
            return Decision(False, _ORCH_TMUX_DENIED)
        if _runs_human_only(seg, plain):
            return Decision(False, _HUMAN_ONLY_DENIED)
        if _strips_harness_env(seg, plain):
            return Decision(False, _ENV_DENIED)
        if _ADDON_ADMIN.search(plain) or _QUOTED_ADDON_ADMIN.search(seg):
            return Decision(False, _ADDON_ADMIN_DENIED)
        if _NO_VERIFY_COMMIT.search(plain) or _NO_VERIFY_PUSH.search(plain):
            return Decision(False, "--no-verify is not allowed: the git hooks check commit messages for this workspace")
        if _changes_hooks_path(seg, plain):
            return Decision(False, "changing core.hooksPath is not allowed: it switches off the git hooks "
                                   "that check commit messages for this workspace")
        if _sets_git_exec_config(seg, plain):
            return Decision(False, "setting git config that runs programs (fsmonitor, sshCommand, pager, editor, "
                                   "aliases, includes, filters, diff commands, credential helpers) is not allowed: "
                                   "later git commands would run them")
        if not may["commit"] and _COMMIT.search(plain):
            return Decision(False, "agents do not commit in this workspace (git.agent_may.commit is false): "
                                   "prepare the change and tell the user it is ready to commit")
        if not may["push"] and _PUSH.search(plain) and not _PUSH_DRY_RUN.search(plain):
            return Decision(False, "agents do not push in this workspace (git.agent_may.push is false)")
        if not may["open_review"] and _REVIEW.search(plain):
            return Decision(False, f"agents do not open {term}s in this workspace (git.agent_may.open_review is false)")
    if _INTERPRETER.search(cmd) and _ADMIN_PY.search(cmd):
        return Decision(False, _ADDON_ADMIN_DENIED)
    code = _code_text(cmd)  # prose in a data heredoc is not code
    if _drives_orch_as_human(cmd, code):
        return Decision(False, _HUMAN_ONLY_DENIED)
    for m in _XARGS_ORCH.finditer(_unquoted(code)):
        sub = m.group(2)
        if not sub or sub in _HUMAN_VERBS or sub in ("move", "permit", "dark", "factory") or sub.startswith(("$", "{", "`", "|", ";", "&")):
            return Decision(False, _HUMAN_ONLY_DENIED)
    if _DECODED_RUN.search(code):
        return Decision(False, _DECODED_DENIED)
    if (_user_addon_path(cmd) or _config_dir_indirect(cmd)) and _is_write(cmd):
        return Decision(False, _ADDON_ADMIN_DENIED)
    if _touches_state(ws, code, cwd) and _is_write(cmd):
        return Decision(False, _USE_ORCH)
    if _STARTUP_FILE.search(code) and _is_write(cmd):
        return Decision(False, _STARTUP_DENIED)
    if _GIT_INTERNAL_PATH.search(_paths_text(code)) and _is_git_write(cmd):
        return Decision(False, _GIT_DIR_DENIED)
    if _GLOBAL_GIT_CONFIG.search(_paths_text(code)) and _is_git_write(cmd):
        return Decision(False, _GLOBAL_GIT_DENIED)
    if _harness_in_text(cmd, ws):
        return Decision(False, _HARNESS_DENIED)
    if "config.json" in code and _WIDGETS_WORD.search(code) and _is_write(cmd):
        return Decision(False, _WIDGETS_DENIED)
    return ALLOW


_WIDGETS_WORD = re.compile(r"\bwidgets\b")
_WIDGETS_DENIED = ("widgets.html (whether agent-written HTML runs in ticket widgets) is the human's setting, signed into "
                   "the approval ledger; ask the user to run `orch widget html on` in their own terminal (anyone may "
                   "turn it off with `orch widget html off`)")
# Files that run code in the human's own sessions: shell startup files, direnv's .envrc, git hooks.
_STARTUP_FILE = re.compile(r"(?:^|[\s'\"=/~])\.(?:zshrc|zshenv|zprofile|zlogin|bashrc|bash_profile|bash_login|profile|"
                           r"envrc)\b|\.git[/\\]hooks(?:[/\\]|\b)")
_STARTUP_DENIED = ("agents do not change shell startup files, .envrc or git hooks: they run code in the human's own "
                   "sessions")
def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _apply(text: str, old: str, new: str, replace_all: bool) -> str | None:
    if not old or old not in text:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _proposed(tool: str, tool_input: dict, text: str) -> str | None:
    if tool == "Write":
        return str(tool_input.get("content", ""))
    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    for e in edits or []:
        text = _apply(text, str(e.get("old_string", "")), str(e.get("new_string", "")), bool(e.get("replace_all")))
        if text is None:
            return None
    return text


def _edit(ws, tool: str, tool_input: dict) -> Decision:
    raw = str(tool_input.get("file_path") or "")
    if not raw:
        return ALLOW
    path = Path(raw)
    path = (path if path.is_absolute() else ws.root / path).resolve()
    if _is_user_addon_file(path):
        return Decision(False, _ADDON_ADMIN_DENIED)
    if _STARTUP_FILE.search(str(path)) or _STARTUP_FILE.search(raw):
        return Decision(False, _STARTUP_DENIED)
    if _under(path, ws.state_dir.resolve()):
        return Decision(False, _USE_ORCH)
    if path == (ws.home / "config.json").resolve():
        return _config_edit(tool, tool_input, path)
    if not _under(path, ws.tickets_dir.resolve()):
        return ALLOW
    if path.name == "INDEX.md":
        return Decision(False, "tickets/INDEX.md is generated by `orch index`")
    if not path.exists():
        return Decision(False, "create tickets with `orch new`, not by writing files")
    old_text = path.read_text(encoding="utf-8")
    new_text = _proposed(tool, tool_input, old_text)
    if new_text is None:
        return ALLOW  # the edit does not apply; the tool reports that itself
    try:
        old = parse_ticket(old_text)
    except TicketParseError:
        return Decision(False, "this ticket file does not parse (a file from an older orch needs `orch migrate`); ask the user to repair it")
    try:
        new = parse_ticket(new_text)
    except TicketParseError:
        return Decision(False, "this edit would break the ticket's frontmatter")
    changed = _protected_changes(old.meta, new.meta, freeze_after_approval=True)
    if old.meta.get("external") != new.meta.get("external"):
        changed.append("external (keys are added with `orch link --external`; a key decides who may edit the Ask)")
    if old.meta.get("parent") != new.meta.get("parent") and _approved_epic_side(ws, old.meta, new.meta):
        changed.append("parent (an approved epic's children change only by the human)")
    if changed:
        return Decision(False, f"this edit changes {', '.join(changed)}; {_USE_ORCH}")
    if old.section("Tasks") != new.section("Tasks"):
        return Decision(False, "the Tasks section changes only through `orch task …` (see the orch-tickets skill)")
    from orch.core.evidence import ticked_without_evidence
    unproven = ticked_without_evidence(new, before=old)
    if unproven:
        return Decision(False, "ticking " + ", ".join(f"AC{n}" for n in unproven) + " needs evidence first: add "
                               "`- AC<n>: <what proved it>` to Verification (`orch section set … Verification`)")
    if old.section("Ask") != new.section("Ask") and not agent_wrote_ask(ws, old):  # #24: until approved
        return Decision(False, "the Ask is protected (written by the human, imported from a tracker, linked to a "
                               "tracker key, or its requirements were approved); only the human changes it "
                               "(`orch section set … Ask` in their terminal, or the dashboard)")
    log_problem = _log_change_problem(old.section("Log"), new.section("Log"))
    if log_problem:
        return Decision(False, log_problem)
    return ALLOW


def _widgets_html(text: str):
    """`widgets.html` as the file sets it (False when unset), or None when the text is not a JSON object."""
    import json
    try:
        cfg = json.loads(text)
    except ValueError:
        return None
    widgets = cfg.get("widgets") if isinstance(cfg, dict) else None
    return widgets.get("html", False) if isinstance(widgets, dict) else (False if isinstance(cfg, dict) else None)


def _config_edit(tool: str, tool_input: dict, path: Path) -> Decision:
    """The workspace config is the agent's to edit (repos, prompts, ...), except `widgets.html`: whether agent-written
    HTML runs in ticket widgets is the human's call, like trusting an addon."""
    try:
        old_text = path.read_text(encoding="utf-8")
    except OSError:
        return ALLOW
    new_text = _proposed(tool, tool_input, old_text)
    before, after = _widgets_html(old_text), (None if new_text is None else _widgets_html(new_text))
    if before is not None and after is not None and before != after:
        return Decision(False, _WIDGETS_DENIED)
    return ALLOW


def _approved_epic_side(ws, old: dict, new: dict) -> bool:
    """Whether the old or the new `parent` is an epic whose requirements the human approved: its set of children is
    part of that approval, so an agent never moves a ticket into or out of it (`orch link --epic` says the same)."""
    from orch.core import store
    from orch.core.epics import epic_approved
    from orch.errors import OrchError
    for meta in (old, new):
        ref = meta.get("parent")
        if not isinstance(ref, (str, int)) or not str(ref).strip():
            continue
        try:
            entry = store.resolve(ws, str(ref))
        except OrchError:
            continue
        if entry.meta is not None and epic_approved(entry.meta):
            return True
    return False


# A Log line that names the human as its actor: `[you]`, `[human]`, `[human:you]` (orch writes `[you]` for the human).
_HUMAN_LOG_ACTOR = re.compile(r"\[\s*(?:you|human)\b[^\]]*\]", re.I)


def _log_change_problem(old: str, new: str) -> str | None:
    """#21: agents only append to the Log (`orch log` is the normal way), and never a line that claims a human."""
    before = [line.rstrip() for line in old.split("\n")] if old.strip() else []
    after = [line.rstrip() for line in new.split("\n")] if new.strip() else []
    if after[:len(before)] != before:
        return "the Log is append-only: existing lines are never changed or removed (use `orch log` to add one)"
    if any(_HUMAN_LOG_ACTOR.search(line) for line in after[len(before):]):
        return "a Log line may not claim a human actor: only orch records what the human did"
    return None
