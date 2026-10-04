from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from pathlib import Path

from orch.errors import UsageError, ValidationError

CONFIG_NAME = "config.json"
HOME_NAME = "orchestrator"
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*$")

DEFAULTS: dict = {
    "schema": 1,
    "customer": "",
    "harnesses": ["claude"],
    "id": {"prefix": "L", "pad": 4},
    "external_trackers": [],
    "git": {
        "type": "github",
        "base_url": "",
        "review_term": "PR",
        "repos": {},
        "branch_pattern": "feature/{key}-{slug}",
        "agent_may": {"commit": False, "push": False, "open_review": False},
    },
    "wiki": {"type": "markdown", "base_url": "", "space": ""},
    "commit": {"subject": "{key} {summary}", "body": ["What", "Why", "Risk"], "rollback": False, "forbid_attribution": True},
    "gates": {"plan_skip_sizes": ["xs"], "requirements_skip_sizes": []},
    "claims": {"ttl_hours": 4},
    "artifacts": {"mode": "local"},
    "temporary": {"max_age_days": 14},
    "dashboard": {"host": "127.0.0.1", "port": 8765, "pull_seconds": 60, "theme": "system", "brand": "none", "stale_minutes": 120,
                  "revalidate_days": 30,  # an open or backlog ticket untouched this long is flagged idle (0: off)
                  "authoring_hints": False},  # True: ticket pages link "Widgets for this section" (authoring docs)
    # Start agent (spec §7): the default harness (one of the known harnesses) and the prompt per
    # mode ({key} and {pr} are the only placeholders; ticket text never enters a prompt). What gets
    # launched (terminal, harness argv) is per user only: ~/.config/orch/launch.json, see
    # orch.dashboard.launch; agents can write this file, so it never decides that.
    "agents": {
        "default_harness": "claude",
        "prompts": {
            "refine": "Refine ticket {key} with the orch-refine-ticket skill.",
            "work": "Work on ticket {key} with the orch-work-on-ticket skill.",
            "fix-checks": "Fix the failing checks on {pr} for ticket {key}.",
            "continue": "Continue ticket {key}: read the new answers/feedback first.",
        },
    },
    # Ticket widgets (docs/widgets.md): agent-written HTML/JS in sandboxed frames. On only when a signed human decision
    # in the approval ledger backs it (`orch widget html on`, orch.core.ledger.widgets_html_state); the guard also
    # refuses an agent edit of this key (hooks/guard.py, _config_edit).
    "widgets": {"html": False},
    "suggested_addons": [],
    "sprints": [],  # [{id, name, start, end}] (orch.core.sprints): planning metadata only
    "feedback": {"enabled": True},  # #37: agents may queue redacted reports about orch itself (orch feedback add)
    # AI Factory (#2, docs/factory.md): off until the human switches it on; a factory epic still needs the human's
    # signed `orch approve <epic> requirements --factory`. Dark (phase 5) is not a config value: it is a signed
    # setting of the checkout (`orch factory dark on`, orch.core.permits.dark_on).
    "factory": {"enabled": False},
    # Named checks (orch.core.receipts): what `orch task done --run` runs for a verify line `check:<name>`, step by
    # step. Each project says what its verification takes; the guard refuses an agent edit of this key.
    "checks": {},
    # Quick tasks (orch.core.quick): one-line jobs outside the ticket flow. On while the human has the `quick-tasks`
    # default addon enabled (its settings hold whether agents may add them and the size limit); this block only
    # names the key prefix, how `orch next` offers them, the artifacts per task and when a claim goes stale.
    "quick": {"prefix": "Q", "next": "idle", "max_artifacts": 5, "claim_minutes": 30},
}

_CHECK_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MAX_CHECK_STEPS = 20
# Under the 600 s an agent harness's shell call allows, so the harness never kills a run before orch does; a check
# that needs longer says so itself (`checks.<name>.timeout`).
DEFAULT_CHECK_TIMEOUT = 540


def repo_git(cfg: dict, name: str | None) -> tuple[str, str]:
    """(type, base_url) of the git host of repo `name`: `git.repos.<name>.type` / `base_url` over the workspace's
    `git.type` / `git.base_url` (#165). A repo whose type differs from the workspace's never inherits its base_url,
    which belongs to the other host."""
    git = cfg.get("git") if isinstance(cfg.get("git"), dict) else {}
    ws_type = _str(git.get("type")) or "github"
    repos = git.get("repos") if isinstance(git.get("repos"), dict) else {}
    spec = repos.get(name) if name is not None and isinstance(repos.get(name), dict) else {}
    kind = _str(spec.get("type")) or ws_type
    base = _str(spec.get("base_url")) or (_str(git.get("base_url")) if kind == ws_type else "")
    return kind, base.rstrip("/")


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def check_timeout(cfg: dict, name: str | None) -> int | None:
    """The seconds the workspace's check `name` allows (`timeout`, 1-86400), or None when it sets none."""
    check = (cfg.get("checks") or {}).get(name) if name and isinstance(cfg.get("checks"), dict) else None
    t = check.get("timeout") if isinstance(check, dict) else None
    return t if isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= 86400 else None


def check_steps(cfg: dict, name: str) -> tuple[list[dict], bool]:
    """The steps ({name, run}) and keep_going of the workspace's check `name`; UsageError naming the configured
    checks when there is none, or saying what is wrong with it."""
    checks = cfg.get("checks") if isinstance(cfg.get("checks"), dict) else {}
    known = ", ".join(sorted(checks)) or "none configured"
    check = checks.get(name)
    if not _CHECK_NAME.match(name or "") or not isinstance(check, dict):
        raise UsageError(f"no check {name!r} in the workspace config (checks: {known})",
                         hint="add it under `checks` in orchestrator/config.json")
    steps = check.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_CHECK_STEPS:
        raise UsageError(f"check {name!r} needs 1-{MAX_CHECK_STEPS} steps")
    out = []
    for i, s in enumerate(steps, 1):
        if not (isinstance(s, dict) and isinstance(s.get("name"), str) and 0 < len(s["name"].strip()) <= 60
                and isinstance(s.get("run"), str) and 0 < len(s["run"].strip()) <= 2000):
            raise UsageError(f"check {name!r} step {i} needs a name (1-60 characters) and a run command")
        out.append({"name": s["name"].strip(), "run": s["run"].strip()})
    return out, bool(check.get("keep_going"))


def deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def check_prefix(prefix: object) -> None:
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        raise ValidationError(f"id prefix must match [A-Z][A-Z0-9]*, got {prefix!r}")


def find_home(start: Path | None = None, *, use_env: bool = True) -> Path:
    """The workspace home. ORCH_HOME wins over `start` (the guard and permit hooks rely on it); `use_env=False`
    is for callers that name a workspace explicitly, such as a throwaway one in a test."""
    env = os.environ.get("ORCH_HOME") if use_env else None
    if env:
        home = Path(env).expanduser().resolve()
        if not (home / CONFIG_NAME).is_file():
            raise UsageError(f"ORCH_HOME={env} has no {CONFIG_NAME}")
        return home
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if d.name == HOME_NAME and (d / CONFIG_NAME).is_file():
            return d
        if (d / HOME_NAME / CONFIG_NAME).is_file():
            return d / HOME_NAME
    raise UsageError(
        "no orchestrator/config.json found in this directory or its parents",
        hint="run `orch init --customer NAME` in the workspace root or set ORCH_HOME",
    )


def load_config(home: Path) -> dict:
    path = home / CONFIG_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ValidationError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(raw, dict):
        raise ValidationError(f"{path}: must be a JSON object")
    if raw.get("schema") != 1:
        raise ValidationError(f"{path}: unsupported schema {raw.get('schema')!r} (expected 1)")
    cfg = deep_merge(DEFAULTS, raw)
    try:
        check_prefix(cfg["id"]["prefix"])
    except ValidationError as e:
        raise ValidationError(f"{path}: {e.message}") from e
    return cfg


def validate_schema(cfg: dict) -> list[str]:
    import jsonschema  # lazy: heavy import, only needed by `orch check`

    schema = json.loads(Path(__file__).with_name("schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(cfg), key=lambda e: [str(p) for p in e.path])
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
