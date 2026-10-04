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
  $ \\ or a newline) anywhere, split by shlex, whose first tokens equal the rule's, and with none of the argument
  shapes that make a program run other code (--exec, --upload-pack, -c, -e, ...). Compound commands, redirects, pipes,
  substitutions and those arguments never match a prefix rule, only an exact one.
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
    python python3 node perl ruby php lua tclsh deno bun bunx npx uv uvx
    curl wget ssh scp sftp ftp telnet nc socat rsync docker kubectl find awk sed tee dd vim vi nano emacs""".split())
_VERSIONED = re.compile(r"python[\d.]*w?|node\d*|perl[\d.]*|ruby[\d.]*|php[\d.]*")
_PROGRAM = re.compile(r"[A-Za-z0-9_.+/][A-Za-z0-9_.+/-]*")
_GIT_NEVER = frozenset("push reset clean fetch pull clone rebase bisect submodule ls-remote archive config worktree "
                       "remote".split())
_NEVER_PROG = frozenset(("rm", "mv"))
# Arguments that make an allowed program run other code; a command carrying one matches no prefix rule.
_RUNS_CODE = frozenset(("--upload-pack", "--receive-pack", "--exec", "--script-shell", "--shell", "--prefix",
                        "--userconfig", "--node-options", "--require", "--config", "--eval"))
_RUNS_CODE_SHORT = frozenset(("-x", "-c", "-e"))


def rule_id(kind: str, value) -> str:
    """A stable id: the same rule always has the same id."""
    return "R-" + hashlib.sha256(canonical_json({"kind": kind, "rule": value})).hexdigest()[:10]


def text(kind: str, value) -> str:
    """The rule as a command line (a prefix joined as the shell would read it)."""
    return value if kind == "exact" else shlex.join(value)


def _printable(s) -> bool:
    return isinstance(s, str) and bool(s) and all(32 <= ord(c) < 127 for c in s)


def simple_tokens(command) -> list[str] | None:
    """The argv of a single simple command, or None: text outside printable ASCII, any shell metacharacter, or text
    the shell cannot split."""
    if not _printable(command) or any(c in _META for c in command):
        return None
    try:
        words = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None
    return words or None


def _runs_code(words) -> str | None:
    return next((w for w in words if w in _RUNS_CODE_SHORT or w.split("=", 1)[0] in _RUNS_CODE), None)


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
    prog, sub = value[0].rsplit("/", 1)[-1].casefold(), value[1].casefold()
    if prog in _BROAD or _VERSIONED.fullmatch(prog):
        return f"`{prog}` runs or reaches anything: list the exact commands instead"
    if prog in _NEVER_PROG:
        return f"`{prog}` is never a prefix rule: list the exact command instead"
    if prog == "git" and sub.startswith("-"):
        return "a git prefix names its subcommand first (an option before it reaches every one)"
    if prog == "git" and sub in _GIT_NEVER:
        return f"`git {sub}` is never a prefix rule: list the exact command instead"
    if prog == "gh" and (sub.startswith("-") or sub == "api"):
        return "a gh prefix names a subcommand other than api"
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


def rules(ws, signed=None) -> list[dict]:
    """The rules in force in this checkout, oldest first: {id, kind, rule, at, actor}. An entry of another checkout,
    one that does not hold together (an id that is not its rule's) or a rule that fails `refusal` is ignored; a
    remove counts only for a rule in force; a cut ledger holds none."""
    from orch.core.ledger import checkout_id
    from orch.core.permits import _signed
    cid = checkout_id(ws)
    out: dict[str, dict] = {}
    for e in _signed(ws, signed):
        if e.get("kind") != KIND or e.get("checkout") != cid:
            continue
        rid, kind, value = e.get("rule_id"), e.get("rule_kind"), e.get("rule")
        if e.get("op") == "remove":
            if isinstance(rid, str) and rid in out:
                del out[rid]
        elif (e.get("op") == "add" and refusal(kind, value) is None and rid == rule_id(kind, value)):
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
