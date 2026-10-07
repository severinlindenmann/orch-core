from __future__ import annotations

import re
import subprocess
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


def _key_pattern(cfg: dict, quick_on: bool = False) -> str:
    from orch.core.trackers import plain
    parts = [rf"{re.escape(cfg['id']['prefix'])}-\d+"] + [f"(?:{plain(t['pattern'])})" for t in cfg["external_trackers"]]
    if quick_on:  # quick-task keys (orch.core.quick) while the quick-tasks addon is on
        from orch.core.quick import key_pattern_of
        parts.append(key_pattern_of(cfg))
    return "|".join(parts)


def subject_regex(cfg: dict, quick_on: bool = False) -> re.Pattern:
    pattern = re.escape(cfg["commit"]["subject"])
    pattern = pattern.replace(re.escape("{key}"), f"(?P<key>{_key_pattern(cfg, quick_on)})")
    pattern = pattern.replace(re.escape("{summary}"), r"(?P<summary>\S.*)")
    return re.compile(pattern)


CHECK_MODES = ("enforce", "warn", "off")


def _repo_spec(cfg: dict, repo: str | None) -> dict:
    git = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    repos = git.get("repos") if isinstance(git.get("repos"), dict) else {}
    spec = repos.get(repo) if repo is not None else None
    return spec if isinstance(spec, dict) else {}


def commit_check_mode(cfg: dict, repo: str | None) -> str:
    """`git.repos.<repo>.commit_check` (#170): enforce (default), warn or off. Anything else, and a repo that is
    not listed, is enforce: the strict check is the one a typo must not switch off."""
    mode = _repo_spec(cfg, repo).get("commit_check")
    return mode if mode in CHECK_MODES else "enforce"


def _skip_entries(cfg: dict, repo: str | None) -> list[tuple[str, object]]:
    commit = cfg.get("commit") if isinstance(cfg.get("commit"), dict) else {}
    out = [(f"commit.skip[{i}]", p) for i, p in enumerate(commit.get("skip") or [])] if isinstance(commit.get("skip"), list) else []
    spec = _repo_spec(cfg, repo)
    if isinstance(spec.get("commit_skip"), list):
        out += [(f"git.repos.{repo}.commit_skip[{i}]", p) for i, p in enumerate(spec["commit_skip"])]
    return out


def commit_skip_patterns(cfg: dict, repo: str | None) -> list[re.Pattern]:
    """The allowed subject patterns of a repo: the global `commit.skip` and its own `commit_skip`. A pattern that is
    not a valid regex matches nothing (`commit_skip_problems` reports it)."""
    out = []
    for _, p in _skip_entries(cfg, repo):
        try:
            out.append(re.compile(p))
        except (re.error, TypeError):
            continue
    return out


def commit_skip_problems(cfg: dict) -> list[str]:
    """One message per allowed pattern (global and per repo) that is not a valid regular expression."""
    git = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    repos = git.get("repos") if isinstance(git.get("repos"), dict) else {}
    seen, out = set(), []
    for repo in (None, *repos):
        for where, p in _skip_entries(cfg, repo):
            if where in seen or not isinstance(p, str):
                continue  # the schema reports a non-string
            seen.add(where)
            try:
                re.compile(p)
            except re.error as e:
                out.append(f"{where}: {p!r} is not a valid regular expression ({e})")
    return out


def repo_name_for(ws, cwd: Path) -> str | None:
    """The git.repos name of the repository `cwd` is in (its working tree, or a linked worktree of it), or None."""
    from orch.hooks.install import configured_repos
    here = cwd.resolve()
    names = list((ws.config["git"].get("repos") or {}))
    for name, path in zip(names, configured_repos(ws)):
        if here == path or here.is_relative_to(path):
            return name
    try:
        common = subprocess.run(["git", "-C", str(here), "rev-parse", "--git-common-dir"], capture_output=True,
                                text=True, encoding="utf-8", timeout=15, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not common:
        return None
    mine = (here / common).resolve()
    for name, path in zip(names, configured_repos(ws)):
        if (path / ".git").resolve() == mine:
            return name
    return None


def effective_mode(ws, repo: str | None) -> str:
    """The mode that applies to this commit: an agent's commit is always enforced (#170), so a relaxed repo relaxes
    only what a human or a tool commits."""
    from orch.actor import agent_harness
    return "enforce" if agent_harness() else commit_check_mode(ws.config, repo)


def check_message(ws, text: str, cwd: Path | None = None, repo: str | None = None) -> list[str]:
    """Problems with this commit message; `cwd` is where git runs the hook, to read which paths are staged, and
    `repo` the git.repos name whose allowed patterns (commit_skip) also let a subject through."""
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
    from orch.actor import agent_harness
    if not agent_harness() and any(rx.search(subject) for rx in commit_skip_patterns(cfg, repo)):
        return problems  # #170: a known tool-generated subject; an agent's commit always needs the full format
    # #168: a commit of nothing but orch's own records is bookkeeping, not code: no plan gate
    if _RECORDS.fullmatch(subject):
        if not records_only(ws, cwd or Path.cwd()):
            problems.append("an 'orch: records' commit may stage only orch records (tickets, gates, events, synced "
                            "instructions); commit code under a ticket key, or run `orch records commit`")
        return problems
    from orch.core import quick
    quick_on = quick.enabled(ws)
    m = subject_regex(cfg, quick_on).fullmatch(subject)
    if not m:
        example = commit_example(cfg).splitlines()[0]
        problems.append(f"subject must match '{cfg['commit']['subject']}', e.g. '{example}'")
        return problems
    key = m.group("key")
    if quick_on and quick.is_key(ws, key):
        problem = quick.commit_problem(ws, key)
        if problem:
            problems.append(problem)
        body = rest.strip()
        for name in body_names(cfg):
            if not re.search(rf"(?m)^{re.escape(name)}:[ \t]*\S", body):
                problems.append(f"body needs a '{name}:' line")
        return problems
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
