"""Dark AI Factory (phase 5, docs/factory.md): the Dark profile, the human's standing list of shell commands a Dark
factory epic may run without asking.

A Dark epic is a factory epic whose signed charter carries `dark: True` (`orch approve <epic> requirements --factory
--dark`, human only) while `factory.enabled` and `factory.dark` are on (orch.core.permits.dark_delegation). In its
sessions the permission hook answers `allow` when a rule here matches the command, and `deny` with a card otherwise;
nothing is ever asked in the session.

- The profile is per workspace: signed ledger entries (kind "dark_profile", op add or remove, the rule's id, kind and
  value), written only by a human process (lifecycle.require_human; ledger.record refuses every other actor). The rules
  in force are a replay of those entries. A cut ledger backs no rule (permits._signed), so the profile does not apply.
- `exact`: the full command text, matched character for character.
- `prefix`: a list of argv tokens. It matches only a single simple command: no shell metacharacter (; & | < > ( ) `
  $ \\ or a newline) anywhere, split by shlex, whose first tokens equal the rule's. Compound commands, redirects,
  pipes and substitutions never match a prefix rule, only an exact one.
- Broad rules are refused when added: a prefix of fewer than two tokens, one that starts with a shell, an interpreter,
  a wrapper or a network or file-sweeping tool, `git` with an option before its subcommand, `git push|reset|clean`,
  `rm` and `mv`, and any rule whose text is never grantable (orch.core.permits.never_grantable). That check also runs
  on every command at match time: a never-grantable command is never allowed, whatever the ledger lists.
"""
from __future__ import annotations

import hashlib
import shlex

from orch.core.canonical import canonical_json
from orch.errors import NotFoundError, UsageError, ValidationError

KIND = "dark_profile"
RULE_KINDS = ("exact", "prefix")
_META = frozenset(";&|<>()`$\\\n")
_BROAD = frozenset("sh bash zsh fish dash env sudo su doas eval exec xargs python python3 node perl ruby osascript "
                   "curl wget ssh scp rsync docker kubectl find awk sed tee dd".split())
_NEVER_PREFIX = (("git", "push"), ("git", "reset"), ("git", "clean"), ("rm",), ("mv",))


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


def check_rule(ws, kind: str, value) -> tuple[str, object]:
    """The rule as it would be signed ((kind, value): a prefix given as text is split into tokens), or a refusal."""
    from orch.core.permits import never_grantable
    if kind not in RULE_KINDS:
        raise UsageError(f"a rule is one of {', '.join(RULE_KINDS)}")
    if not value or (isinstance(value, str) and not value.strip()):
        raise ValidationError("the rule is empty")
    if kind == "prefix" and isinstance(value, str):
        value = simple_tokens(value)
        if value is None:
            raise ValidationError("a prefix rule is plain words: no shell metacharacters, quotes that close, printable "
                                  "ASCII only")
    if kind == "exact":
        if not _printable(value):
            raise ValidationError("the rule holds characters outside printable ASCII or spans several lines")
    else:
        if not isinstance(value, list) or not all(_printable(w) and not any(c in _META for c in w) for w in value):
            raise ValidationError("a prefix rule is plain words: no shell metacharacters, printable ASCII only")
        if len(value) < 2:
            raise ValidationError("a prefix rule needs at least two words (a program alone allows everything it does)")
        prog = value[0].rsplit("/", 1)[-1]
        if prog in _BROAD:
            raise ValidationError(f"`{prog}` runs or reaches anything: list the exact commands instead")
        if prog == "git" and value[1].startswith("-"):
            raise ValidationError("a git prefix names its subcommand first (an option before it reaches every one)")
        words = (prog, *value[1:])
        for p in _NEVER_PREFIX:
            if words[:len(p)] == p:
                raise ValidationError(f"`{' '.join(p)}` is never a prefix rule: list the exact command instead")
    why = never_grantable(ws, text(kind, value))
    if why:
        raise ValidationError(f"this can never be in the Dark profile: {why}")
    return kind, value


def rules(ws, signed=None) -> list[dict]:
    """The rules in force, oldest first: {id, kind, rule, at, actor}. An entry that does not hold together (an id that
    is not its rule's) is skipped; a cut ledger holds none."""
    from orch.core.permits import _signed
    out: dict[str, dict] = {}
    for e in _signed(ws, signed):
        if e.get("kind") != KIND:
            continue
        rid, kind, value = e.get("rule_id"), e.get("rule_kind"), e.get("rule")
        if e.get("op") == "remove":
            out.pop(rid, None)
        elif (e.get("op") == "add" and kind in RULE_KINDS and rid == rule_id(kind, value)
              and (isinstance(value, str) if kind == "exact" else isinstance(value, list)
                   and all(isinstance(w, str) for w in value))):
            out[rid] = {"id": rid, "kind": kind, "rule": value, "at": e.get("at"), "actor": e.get("actor")}
    return list(out.values())


def match(ws, command, listed=None) -> dict | None:
    """The rule that lets `command` run in a Dark epic, or None. Never one for a never-grantable command."""
    from orch.core.permits import never_grantable
    listed = rules(ws) if listed is None else listed
    if not listed or not isinstance(command, str) or never_grantable(ws, command):
        return None
    words = simple_tokens(command)
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
                         rule_kind=kind, rule=value)


def add(ws, actor, kind: str, value) -> dict:
    """Human only: sign a rule into this workspace's Dark profile."""
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
    """Human only: an open Dark request (source "dark") of this workspace becomes an exact rule of its command text.
    `expected_sha`: the sha256 of the command the human was shown."""
    from orch.core import permits
    permits._human_check(actor, "changing the Dark profile")
    r = permits._open_request(ws, request_id, expected_sha)
    if r["source"] != "dark":
        raise ValidationError(f"{r['id']} was not filed by a Dark factory: grant or deny it instead",
                              hint=f"orch permit grant {r['id']}")
    return add(ws, actor, "exact", r["command"])
