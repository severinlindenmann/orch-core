from __future__ import annotations

import re
from pathlib import Path

from orch.core import store
from orch.core.gates import gate_state, plan_required
from orch.core.gitfiles import records_only
from orch.errors import NotFoundError, TicketParseError, UsageError
from orch.instructions.render import body_names, commit_example

ATTRIBUTION_PATTERNS = (
    re.compile(r"(?im)^[\w-]+-by:.*(claude|anthropic|copilot|openai|gpt|codex|gemini|cursor)"),
    re.compile(r"(?i)\bgenerated (with|by)\b"),
    re.compile("\U0001F916"),
)
_SKIP_FORMAT = re.compile(r"^(Merge |Revert |fixup! |squash! |amend! )")
_SCISSORS = re.compile(r"^# -+ >8 -+$")
_RECORDS = re.compile(r"orch: records(?: \S.*)?")


def clean_message(text: str) -> str:
    """The message as git will store it: no comment lines, nothing below the scissors line."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if _SCISSORS.match(line):
            break
        if line.startswith("#"):
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip()


def _key_pattern(cfg: dict) -> str:
    from orch.core.trackers import plain
    parts = [rf"{re.escape(cfg['id']['prefix'])}-\d+"] + [f"(?:{plain(t['pattern'])})" for t in cfg["external_trackers"]]
    return "|".join(parts)


def subject_regex(cfg: dict) -> re.Pattern:
    pattern = re.escape(cfg["commit"]["subject"])
    pattern = pattern.replace(re.escape("{key}"), f"(?P<key>{_key_pattern(cfg)})")
    pattern = pattern.replace(re.escape("{summary}"), r"(?P<summary>\S.*)")
    return re.compile(pattern)


def check_message(ws, text: str, cwd: Path | None = None) -> list[str]:
    """Problems with this commit message; `cwd` is where git runs the hook, to read which paths are staged."""
    cfg = ws.config
    msg = clean_message(text)
    if not msg:
        return ["the commit message is empty"]
    problems = []
    if cfg["commit"]["forbid_attribution"]:
        for rx in ATTRIBUTION_PATTERNS:
            m = rx.search(msg)
            if m:
                problems.append(f"remove the AI attribution ({m.group(0).strip()!r}); this workspace forbids it")
    subject, _, rest = msg.partition("\n")
    if _SKIP_FORMAT.match(subject):
        return problems
    # #168: a commit of nothing but orch's own records is bookkeeping, not code: no plan gate
    if _RECORDS.fullmatch(subject):
        if not records_only(ws, cwd or Path.cwd()):
            problems.append("an 'orch: records' commit may stage only orch records (tickets, gates, events, synced "
                            "instructions); commit code under a ticket key, or run `orch records commit`")
        return problems
    m = subject_regex(cfg).fullmatch(subject)
    if not m:
        example = commit_example(cfg).splitlines()[0]
        problems.append(f"subject must match '{cfg['commit']['subject']}', e.g. '{example}'")
        return problems
    key = m.group("key")
    try:
        _, ticket = store.load(ws, key)
    except NotFoundError:
        problems.append(f"no ticket {key} (create it with `orch new` or link the key with `orch link --external`)")
    except (UsageError, TicketParseError) as e:
        problems.append(f"{key}: {e.message}")
    else:
        if (plan_required(ws, ticket) and gate_state(ticket, "plan") != "approved"
                and not records_only(ws, cwd or Path.cwd())):
            problems.append(f"{ticket.id}: plan is not approved yet (size {ticket.meta.get('size')}); "
                            f"the human runs `orch approve {ticket.id} plan`")
    body = rest.strip()
    for name in body_names(cfg):
        if not re.search(rf"(?m)^{re.escape(name)}:[ \t]*\S", body):
            problems.append(f"body needs a '{name}:' line")
    return problems
