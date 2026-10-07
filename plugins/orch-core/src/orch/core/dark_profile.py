"""Dark AI Factory (phase 5, docs/factory.md): the Dark profile, the human's standing list of shell commands a Dark
factory epic may run without asking.

A Dark epic is a factory epic whose signed charter carries `dark: True` (`orch approve <epic> requirements --dark`,
human only) while `factory.enabled` is on and this checkout's signed Dark switch is on (`orch factory dark on`;
orch.core.permits.dark_on, dark_delegation). In its sessions the permission hook answers `allow` when a rule here
matches the command, and `deny` with a card otherwise; nothing is ever asked in the session.

- The profile is per checkout: signed ledger entries (kind "dark_profile", op add or remove, the rule's id, kind and
  value, and the checkout they were made in, ledger.checkout_id, as for signed settings), written only by a human
  process (lifecycle.require_human; ledger.record refuses every other actor). The rules in force are a replay of the
  entries of this workspace and checkout; each replayed rule is checked again as when it was added, and one that fails
  is ignored. A cut ledger backs no rule (permits._signed), so the profile does not apply.
- `exact`: the full command text, matched character for character.
- `prefix`: a list of argv tokens. It matches only a single simple command: no shell metacharacter (; & | < > ( ) `
  $ \\ or a newline), glob, brace or tilde outside quotes, nothing still active inside double quotes ($ ` \\ !), split
  by shlex, whose first tokens equal the rule's, and with none of the argument shapes that make a program run other
  code (--exec, --upload-pack, -c, -e, ...). Compound commands, redirects, pipes, substitutions and those arguments
  never match a prefix rule, only an exact one. Quoted text is plain text to the shell, so `-m "a (b); c"` matches.
- Broad rules are refused when added (and ignored when replayed): a prefix of fewer than two tokens, one whose program
  is not a plain word, is an environment assignment, or is a shell, an interpreter, a wrapper, an editor or a network
  or file-sweeping tool, `git` with an option or a fetching, rewriting or configuring subcommand first, `gh api`,
  `rm` and `mv`, and any rule whose text is never grantable (orch.core.permits.never_grantable). That check also runs
  on every command at match time: a never-grantable command is never allowed, whatever the ledger lists.
"""
from __future__ import annotations

import hashlib
import re
import shlex

from orch.core.canonical import canonical_json
from orch.errors import NotFoundError, UsageError, ValidationError

KIND = "dark_profile"
RULE_KINDS = ("exact", "prefix")
_META = frozenset(";&|<>()`$\\\n")
_BROAD = frozenset("""sh bash zsh fish dash ksh csh tcsh pwsh busybox env sudo su doas eval exec xargs nohup time nice
    timeout watch command builtin arch xcrun caffeinate script tmux screen osascript open launchctl crontab at
    stdbuf ionice setsid flock unbuffer parallel expect gdb lldb sqlite3 less man
    python python3 py node perl ruby php lua tclsh deno bun bunx npx uv uvx irb julia rscript swift jshell
    curl wget ssh scp sftp ftp telnet nc socat rsync docker kubectl find awk sed tee dd vim vi nano emacs""".split())
# Families by name (casefolded, a trailing .exe removed): shells, interpreters and their versions, awk/sed/find.
_BROAD_RE = re.compile(r"(?:ba|z|k|c|tc|da|fi)?sh[\d.]*|python[\d.]*w?(?:-\S+)?|pypy[\d.]*|ipython[\d.]*"
                       r"|node(?:js)?[\d.-]*|perl[\d.]*|ruby[\d.]*|php[\d.]*|[gmn]?awk|g?sed|g?find")
_PROGRAM = re.compile(r"[A-Za-z0-9_.+/][A-Za-z0-9_.+/-]*")
_GIT_NEVER = frozenset("""push reset clean fetch pull clone rebase bisect submodule ls-remote archive config worktree
    remote grep difftool mergetool filter-branch daemon instaweb send-email credential p4 svn update-ref replace gc
    branch checkout""".split())
# (program, subcommand) pairs that run or fetch arbitrary code.
_SUB_NEVER = {"npm": {"exec", "x", "dlx"}, "pnpm": {"exec", "x", "dlx"}, "yarn": {"exec", "x", "dlx"},
              "cargo": {"run"}, "go": {"run"}, "gh": {"alias", "extension", "secret", "api"}}
_NEVER_PROG = frozenset(("rm", "mv"))
# Some argument shapes that make an allowed program run other code, read other config or write elsewhere; a command
# carrying one matches no prefix rule. Not every such argument: a prefix rule trusts the repository (docs/factory.md).
_RUNS_CODE = frozenset(("--upload-pack", "--receive-pack", "--exec", "--script-shell", "--shell", "--prefix",
                        "--userconfig", "--node-options", "--require", "--config", "--eval", "--workspace",
                        "--globalconfig", "--open-files-in-pager", "--ext-diff", "--textconv", "--output", "--file",
                        "--makefile", "--rootdir", "--confcutdir", "--manifest-path", "--to-command",
                        "--use-compress-program", "--checkpoint-action"))
_SHORT_LETTERS = frozenset("cexCwfpoIO")  # a single-dash word holding one of these (-x, -xc, -Ofoo, -Ipath, ...)
_ASSIGN = ("SHELL=", "MAKEFLAGS=")


def _prog(word: str) -> str:
    p = word.rsplit("/", 1)[-1].casefold()
    return p[:-4] if p.endswith(".exe") else p


# git reads files and rewrites commits through these (git accepts any unambiguous abbreviation of a long option): a
# git command carrying one, a short option word holding F or t, or any argument naming a path outside the repository
# (absolute, ~, or with a .. component) matches no prefix rule.
_GIT_REFUSED = frozenset(("--no-index", "--pathspec-from-file", "--template", "--orderfile", "--amend", "--fixup",
                          "--squash", "--file"))
_GIT_SHORT = frozenset("Ft")


def _git_bad(w: str, message: bool = False, commit: bool = False) -> bool:
    """`message`: `w` is the text of a commit message (the word after -m or --message of `git commit`): its text is
    not a path, so the path checks are skipped, every option check stays. `commit`: the command is `git commit`."""
    if w.startswith("--") and len(w) > 2:
        name = "--" + w[2:].split("=", 1)[0].casefold()
        if any(o.startswith(name) for o in _GIT_REFUSED):
            return True
        if commit and name in ("--message", "--mess", "--messa", "--messag"):
            return False  # --message=<text>: the rest is message text
    elif w.startswith("-") and len(w) > 1 and any(c in _GIT_SHORT for c in w[1:]):
        return True
    elif commit and re.fullmatch(r"-[a-zA-Z]*m.*", w):
        return False  # -m<text> or -am<text> of git commit: what follows m is message text
    if message:
        return False
    for v in (w, w.split("=", 1)[1] if "=" in w else "", w.split(":", 1)[1] if ":" in w else ""):
        if v.startswith(("/", "~")) or ".." in v.split("/"):
            return True
    return False


def _bad_arg(w: str, orch: bool = False) -> bool:
    if w.startswith(_ASSIGN):
        return True
    if w.startswith("--") and len(w) > 2:
        name = "--" + w[2:].split("=", 1)[0].casefold().replace("_", "-")
        if orch and name == "--file":
            return False  # orch reads a bound session's files only inside the workspace (fsutil agent_source)
        # an option a program accepts abbreviated (npm does) counts as the refused one it abbreviates
        return name in _RUNS_CODE or (len(name) >= 5 and any(o.startswith(name) for o in _RUNS_CODE))
    return w.startswith("-") and len(w) > 1 and any(c in _SHORT_LETTERS for c in w[1:])


def rule_id(kind: str, value) -> str:
    """A stable id: the same rule always has the same id."""
    return "R-" + hashlib.sha256(canonical_json({"kind": kind, "rule": value})).hexdigest()[:10]


def text(kind: str, value) -> str:
    """The rule as a command line (a prefix joined as the shell would read it)."""
    return value if kind == "exact" else shlex.join(value)


def _printable(s) -> bool:
    return isinstance(s, str) and bool(s) and all(32 <= ord(c) < 127 for c in s)


_DQ_ACTIVE = frozenset("$`\\!")  # still active (or history-expanding) inside double quotes
# Outside quotes, besides _META: globs, braces and the tilde expand (a file named `--exec=x` matched by `-*` would
# reach the program as that option), so they refuse too; so do # and ^ anywhere (zsh's extendedglob makes them glob
# operators mid-word), ! (history and negation), and = starting a word (zsh's equals expansion).
_BARE_ACTIVE = _META | frozenset("*?[{}~#^!")


def _inert_quoting(command: str) -> bool:
    """Whether every shell metacharacter of `command` sits inside quotes where the shell reads it as plain text:
    anything inside single quotes; inside double quotes anything but $ ` \\ and !. Outside quotes no metacharacter,
    glob, brace, tilde, #, ^ or !, and no = starting a word. Quotes must close, and a single-quoted piece never
    directly follows another (zsh's rcquotes reads `'a''b'` as a'b). This assumes a plain shell configuration: the
    differential test runs sh, bash, zsh and zsh with extendedglob and rcquotes, not every option a user may set."""
    q, start, closed = None, True, False  # start: nothing but quote marks since the word began (zsh: `""=x` is `=x`)
    for c in command:
        if q == "'":
            q, start, closed = (None, start, True) if c == "'" else (q, False, False)
            continue
        if c == "'" and closed:
            return False
        closed = False
        if q == '"':
            if c in _DQ_ACTIVE:
                return False
            q, start = (None, start) if c == '"' else (q, False)
        elif c in "'\"":
            q = c
        elif c in _BARE_ACTIVE or (start and c == "="):
            return False
        else:
            start = c == " "
    return q is None


def simple_tokens(command) -> list[str] | None:
    """The argv of a single simple command, or None: text outside printable ASCII, a shell metacharacter outside
    quotes (or one still active inside double quotes), a comment, or text the shell cannot split. A metacharacter in
    correctly quoted text (`-m "Elephants (WWF)"`) is plain text to the shell, so it does not refuse the command;
    tests/test_dark_quoting.py checks the split against the real shells."""
    if not _printable(command) or not _inert_quoting(command):
        return None
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None
    return words or None


def _is_orch(word) -> bool:
    """`orch` by name (found on the session's PATH), or exactly the path the runner resolves `orch` to: never another
    path, which could name a script the agent wrote. Note: this runs in the hook's process, whose PATH is the session's
    (the folders of the resolved claude, orch and uv, then the system's), while the runner resolved `orch` on the
    dashboard's PATH; the bare word always means whatever `orch` that session PATH finds first."""
    if word == "orch":
        return True
    from orch.core.factory_runner import resolve_bin
    return isinstance(word, str) and word.startswith("/") and word == resolve_bin("orch")


# Short options that hold a _SHORT_LETTERS letter but, for this git verb, only read: the exact word, nothing attached.
# `git ls-files -o` is `--others` (untracked file names). The commit gate (permits._VERBS) allows the same word.
_GIT_READ_SHORT = {"ls-files": frozenset({"-o"})}


def _runs_code(words) -> str | None:
    orch = bool(words) and _is_orch(words[0])
    git = bool(words) and _prog(words[0]) == "git"
    commit = git and words[1:2] == ["commit"]
    safe = _GIT_READ_SHORT.get(words[1], frozenset()) if git and len(words) > 1 else frozenset()
    for i, w in enumerate(words):
        prev = words[i - 1] if i else ""
        message = commit and (prev in ("-m", "--message") or bool(re.fullmatch(r"-[a-zA-Z]*m", prev)))
        if (_bad_arg(w, orch) and w not in safe) or (git and _git_bad(w, message, commit)):
            return w
    return None


def refusal(kind, value) -> str | None:
    """Why the rule (kind, value) can never be in the profile, judged on its shape alone, or None. The same check
    runs when a rule is added and when a signed entry is replayed."""
    if kind not in RULE_KINDS:
        return f"a rule is one of {', '.join(RULE_KINDS)}"
    if not value or (isinstance(value, str) and not value.strip()):
        return "the rule is empty"
    if kind == "exact":
        return None if _printable(value) else "the rule holds characters outside printable ASCII or spans several lines"
    if not isinstance(value, list) or not all(_printable(w) and not any(c in _META for c in w) for w in value):
        return "a prefix rule is plain words: no shell metacharacters, printable ASCII only"
    if len(value) < 2:
        return "a prefix rule needs at least two words (a program alone allows everything it does)"
    if "=" in value[0] or not _PROGRAM.fullmatch(value[0]):
        return "a prefix rule starts with a plain program name (no variable assignment, no option)"
    prog, sub = _prog(value[0]), value[1].casefold()
    if prog in _BROAD or _BROAD_RE.fullmatch(prog):
        return f"`{prog}` runs or reaches anything: list the exact commands instead"
    if prog in _NEVER_PROG:
        return f"`{prog}` is never a prefix rule: list the exact command instead"
    if prog == "git" and sub.startswith("-"):
        return "a git prefix names its subcommand first (an option before it reaches every one)"
    if prog == "git" and sub in _GIT_NEVER:
        return f"`git {sub}` is never a prefix rule: list the exact command instead"
    if prog == "gh" and sub.startswith("-"):
        return "a gh prefix names its subcommand first"
    if sub in _SUB_NEVER.get(prog, ()):
        return f"`{prog} {sub}` is never a prefix rule: list the exact command instead"
    flag = _runs_code(value)
    if flag:
        return f"`{flag}` makes a program run other code: list the exact command instead"
    return None


def check_rule(ws, kind: str, value) -> tuple[str, object]:
    """The rule as it would be signed ((kind, value): a prefix given as text is split into tokens), or a refusal."""
    from orch.core.permits import never_grantable
    if kind not in RULE_KINDS:
        raise UsageError(f"a rule is one of {', '.join(RULE_KINDS)}")
    if kind == "prefix" and isinstance(value, str) and value.strip():
        value = simple_tokens(value)
        if value is None:
            raise ValidationError("a prefix rule is plain words: no shell metacharacters, quotes that close, printable "
                                  "ASCII only")
    why = refusal(kind, value)
    if why:
        raise ValidationError(why)
    why = never_grantable(ws, text(kind, value))
    if why:
        raise ValidationError(f"this can never be in the Dark profile: {why}")
    return kind, value


def rules(ws, signed=None, checkout: str | None = None) -> list[dict]:
    """The rules in force in this checkout (or in `checkout`, the id a session binding recorded), oldest first: {id,
    kind, rule, at, actor}. An entry of another checkout, one that does not hold together (an id that is not its
    rule's) or a rule that fails `refusal` is ignored; a remove counts only for a rule in force; a cut ledger holds
    none."""
    from orch.core.ledger import checkout_id
    from orch.core.permits import _signed
    cid = checkout or checkout_id(ws)
    out: dict[str, dict] = {}
    for e in _signed(ws, signed):
        if e.get("kind") != KIND or e.get("checkout") != cid:
            continue
        rid, kind, value = e.get("rule_id"), e.get("rule_kind"), e.get("rule")
        if e.get("op") == "remove":
            if isinstance(rid, str) and rid in out:
                del out[rid]
        elif e.get("op") == "add" and refusal(kind, value) is None and isinstance(rid, str) and rid == rule_id(kind, value):
            out[rid] = {"id": rid, "kind": kind, "rule": value, "at": e.get("at"), "actor": e.get("actor")}
    return list(out.values())


def match(ws, command, listed=None) -> dict | None:
    """The rule that lets `command` run in a Dark epic, or None. Never one for a never-grantable command."""
    from orch.core.permits import never_grantable
    listed = rules(ws) if listed is None else listed
    if not listed or not isinstance(command, str) or never_grantable(ws, command):
        return None
    words = simple_tokens(command)
    if words is not None and _runs_code(words):
        words = None  # such a command can only match an exact rule
    for r in listed:
        if r["kind"] == "exact" and r["rule"] == command:
            return r
        if r["kind"] == "prefix" and words is not None and words[:len(r["rule"])] == r["rule"]:
            return r
    return None


def _sign(ws, actor, op: str, rid: str, kind: str, value) -> dict:
    from orch.actor import process_evidence
    from orch.core import ledger
    from orch.core.permits import _human_check
    _human_check(actor, "changing the Dark profile")
    return ledger.record(ws, ticket=None, kind=KIND, actor=actor, evidence=process_evidence(), op=op, rule_id=rid,
                         rule_kind=kind, rule=value, checkout=ledger.checkout_id(ws))


def add(ws, actor, kind: str, value) -> dict:
    """Human only: sign a rule into this checkout's Dark profile."""
    from orch.core.permits import _human_check
    _human_check(actor, "changing the Dark profile")
    kind, value = check_rule(ws, kind, value)
    rid = rule_id(kind, value)
    if any(r["id"] == rid for r in rules(ws)):
        raise ValidationError(f"{rid} is in the Dark profile already")
    return _sign(ws, actor, "add", rid, kind, value)


# The baseline: prefix rules for the orch verbs an unattended planner or worker session runs. Agent verbs only; no
# human-only verb (approve, answer, verdict, request-changes, reopen, close, ledger, epic pause, permit grant/deny/revoke,
# dark profile add/remove, factory dark on, addon admin, serve) and no `ask` (refused in a factory epic). A human-only
# form of an allowed verb (`orch move X done`) is still denied: match() refuses every never-grantable command, and the
# guard denies those. tests/test_factory_planner.py checks each rule against the CLI's real commands.
BASELINE = ("orch show", "orch list", "orch search", "orch next", "orch state", "orch check", "orch new",
            "orch section set", "orch task add", "orch task start", "orch task done", "orch task skip",
            "orch task block", "orch task list", "orch claim", "orch release", "orch log", "orch link", "orch move",
            "orch wait", "orch permit request", "orch permit list", "orch permit show", "orch artifact add",
            "orch epic show", "orch epic auto-approve")
# The git a worker needs to commit its own work, read-only verbs and add/commit only: no push, fetch, reset, clean,
# checkout, switch, rebase, config or `-c` (refusal() refuses those as prefixes anyway). `git branch` is never a
# prefix rule, so its read-only form is an exact one.
GIT_BASIC = ("git status", "git diff", "git log", "git show", "git add", "git commit", "git ls-tree", "git ls-files",
             "git rev-parse")
GIT_BASIC_EXACT = ("git branch --show-current",)
BASELINES = {"orch": tuple(("prefix", r) for r in BASELINE),
             "git-basic": (*(("prefix", r) for r in GIT_BASIC), *(("exact", r) for r in GIT_BASIC_EXACT))}


def _baseline(name: str):
    if name not in BASELINES:
        raise UsageError(f"no baseline {name!r}: the baselines are {', '.join(BASELINES)}")
    return BASELINES[name]


def _id_of(kind: str, r: str) -> str:
    return rule_id(kind, r.split() if kind == "prefix" else r)


def baseline_todo(ws, name: str = "orch") -> list[str]:
    """The rules of baseline `name` not in force in this checkout yet, in order (their text)."""
    have = {r["id"] for r in rules(ws)}
    return [r for kind, r in _baseline(name) if _id_of(kind, r) not in have]


def baseline_kind(name: str, text_: str) -> str:
    return next(kind for kind, r in _baseline(name) if r == text_)


def add_baseline(ws, actor, shown=None, name: str = "orch") -> dict:
    """Human only: sign every rule of baseline `name` not in force yet (and, with `shown`, among the rules the human
    was shown), each through `add` (the same checks). {added: the entries signed, failed: [{rule, error}]}: one rule
    that fails does not hide which others were signed."""
    from orch.core.permits import _human_check
    from orch.errors import OrchError
    _human_check(actor, "changing the Dark profile")
    added, failed = [], []
    for r in baseline_todo(ws, name):
        if shown is not None and r not in shown:
            continue
        try:
            added.append(add(ws, actor, baseline_kind(name, r), r))
        except (OrchError, OSError) as e:
            failed.append({"rule": r, "error": str(e)})
    return {"added": added, "failed": failed}


def compound(command) -> bool:
    """Whether `command` chains, pipes, redirects or substitutes: an unquoted ; & | < > ( ) or line break, or a
    backtick or $( outside single quotes. No prefix rule ever matches it, and an exact rule only its identical text.
    (`ls ~/x` or `pytest tests/*.py` are not compound: they only match no prefix rule.)"""
    if not isinstance(command, str):
        return False
    q, prev, esc = None, "", False
    for c in command:
        if esc:  # a backslash-escaped character (\; of find -exec, \" in double quotes) is plain
            esc, prev = False, ""
            continue
        if q == "'":
            q = None if c == "'" else q
        elif c == "\\":
            esc = True
        elif c == "`" or (c == "(" and prev == "$") or c == "\n":
            return True
        elif q == '"':
            q = None if c == '"' else q
        elif c in "'\"":
            q = c
        elif c in ";&|<>()":
            return True
        prev = c if q != "'" else ""
    return False


def prunable(ws) -> list[dict]:
    """Exact rules whose command is compound: junk from adding one chained card, which agents rarely repeat."""
    return [r for r in rules(ws) if r["kind"] == "exact" and compound(r["rule"])]


def prune(ws, actor, shown: list[str]) -> list[dict]:
    """Human only: remove the prunable rules among the ids the human was shown; the entries signed."""
    from orch.core.permits import _human_check
    _human_check(actor, "changing the Dark profile")
    return [remove(ws, actor, r["id"]) for r in prunable(ws) if r["id"] in shown]


def remove(ws, actor, rid: str) -> dict:
    """Human only: take a rule out; the next Dark prompt for it is denied and becomes a card."""
    from orch.core.permits import _human_check
    _human_check(actor, "changing the Dark profile")
    r = next((x for x in rules(ws) if x["id"] == rid), None)
    if r is None:
        raise NotFoundError(f"no rule {rid} in the Dark profile")
    return _sign(ws, actor, "remove", rid, r["kind"], r["rule"])


def add_from_request(ws, actor, request_id: str, *, expected_sha: str | None) -> dict:
    """Human only: an open Dark request (source "dark") of this workspace, whose epic is still an active Dark epic,
    becomes an exact rule of its command text. `expected_sha`: the sha256 of the command the human was shown."""
    from orch.core import permits, store
    permits._human_check(actor, "changing the Dark profile")
    r = permits._open_request(ws, request_id, expected_sha)
    if r["source"] != "dark":
        raise ValidationError(f"{r['id']} was not filed by a Dark factory: grant or deny it instead",
                              hint=f"orch permit grant {r['id']}")
    try:
        epic = store.read_ticket(store.resolve(ws, str(r["epic"])).path)
    except Exception:
        epic = None
    if epic is None or permits.dark_delegation(ws, epic) is None:
        raise ValidationError(f"the epic of {r['id']} is not an active Dark factory epic now (or Dark is switched off): "
                              "grant or deny the request instead", hint=f"orch permit grant {r['id']}")
    return add(ws, actor, "exact", r["command"])
