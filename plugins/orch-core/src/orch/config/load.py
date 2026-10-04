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
    "gates": {"plan_skip_sizes": ["xs"]},
    "claims": {"ttl_hours": 4},
    "artifacts": {"mode": "local"},
    "temporary": {"max_age_days": 14},
    "dashboard": {"host": "127.0.0.1", "port": 8765, "pull_seconds": 60, "theme": "system", "brand": "none", "stale_minutes": 120},
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
    # signed `orch approve <epic> requirements --factory`. `dark` (phase 5): a factory epic the human started with
    # `--dark` answers its permission prompts from the signed Dark profile instead of cards (orch.core.dark_profile).
    "factory": {"enabled": False, "dark": False},
}


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


def find_home(start: Path | None = None) -> Path:
    env = os.environ.get("ORCH_HOME")
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
